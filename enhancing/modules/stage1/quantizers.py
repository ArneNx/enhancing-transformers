# ------------------------------------------------------------------------------------
# Enhancing Transformers
# Copyright (c) 2022 Thuan H. Nguyen. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------------------
# Modified from Taming Transformers (https://github.com/CompVis/taming-transformers)
# Copyright (c) 2020 Patrick Esser and Robin Rombach and Björn Ommer. All Rights Reserved.
# ------------------------------------------------------------------------------------

from functools import partial
from typing import Tuple, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from ...utils.general import get_obj_from_str, initialize_from_config


class BaseQuantizer(nn.Module):
    def __init__(self, straight_through: bool = True, use_norm: bool = True) -> None:
        super().__init__()
        self.straight_through = straight_through
        self.norm = lambda x: F.normalize(x, dim=-1) if use_norm else x
        
    def quantize(self, z: torch.FloatTensor) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.LongTensor]:
        pass
    
    def forward(self, z: torch.FloatTensor) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.LongTensor]:
        if not self.use_residual:
            z_q, loss, encoding_indices = self.quantize(z)
        else:
            z_q = torch.zeros_like(z)
            residual = z.detach().clone()

            losses = []
            encoding_indices = []

            for _ in range(self.num_quantizers):
                z_qi, loss, indices = self.quantize(residual.clone())
                residual.sub_(z_qi)
                z_q.add_(z_qi)

                encoding_indices.append(indices)
                losses.append(loss)

            losses, encoding_indices = map(partial(torch.stack, dim = -1), (losses, encoding_indices))
            loss = losses.mean()

        # preserve gradients with straight-through estimator
        if self.straight_through:
            z_q = z + (z_q - z).detach()

        return z_q, loss, encoding_indices


class VectorQuantizer(BaseQuantizer):
    def __init__(self, embed_dim: int, n_embed: int, beta: float = 0.25, use_norm: bool = True,
                 use_residual: bool = False, num_quantizers: Optional[int] = None, **kwargs) -> None:
        super().__init__(use_norm=use_norm)
        self.n_embed = n_embed
        self.embed_dim = embed_dim

        self.beta = beta

        self.use_residual = use_residual
        self.num_quantizers = num_quantizers
        
        self.embedding = nn.Embedding(self.n_embed, self.embed_dim)
        self.embedding.weight.data.uniform_(-1.0 / self.n_embed, 1.0 / self.n_embed)

    def quantize(self, z: torch.FloatTensor) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.LongTensor]:
        z_reshaped_norm = self.norm(z.view(-1, self.embed_dim))
        embedding_norm = self.norm(self.embedding.weight)
        
        d = torch.sum(z_reshaped_norm ** 2, dim=1, keepdim=True) + \
            torch.sum(embedding_norm**2, dim=1) - 2 * \
            torch.einsum('b d, n d -> b n', z_reshaped_norm, embedding_norm)

        encoding_indices = torch.argmin(d, dim=1).unsqueeze(1)
        encoding_indices = encoding_indices.view(*z.shape[:2])
        
        z_q = self.embedding(encoding_indices).view(z.shape)
        z_qnorm, z_norm = self.norm(z_q), self.norm(z)
        
        # compute loss for embedding
        loss = torch.mean((z_qnorm.detach()-z_norm)**2) +  \
               self.beta * torch.mean((z_qnorm - z_norm.detach())**2)

        return z_qnorm, loss, encoding_indices


class GumbelQuantizer(BaseQuantizer):
    def __init__(self, embed_dim: int, n_embed: int, temp_init: float = 1.0, kl_weight: float = 5e-4,
                 use_norm: bool = True, use_residual: bool = False, num_quantizers: Optional[int] = None, **kwargs) -> None:
        super().__init__(False, use_norm)
        
        self.embed_dim = embed_dim
        self.n_embed = n_embed

        self.straight_through = straight_through
        self.temperature = temp_init
        self.kl_weight = kl_weight
        
        self.proj = nn.Linear(embed_dim, n_embed, 1)

        self.use_residual = use_residual
        self.num_quantizers = num_quantizers
            
        self.embedding = nn.Embedding(self.n_embed, self.embed_dim)
        self.embedding.weight.data.uniform_(-1.0 / self.n_embed, 1.0 / self.n_embed)
        
    def quantize(self, z: torch.FloatTensor, temp: Optional[float] = None) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.LongTensor]:
        # force hard = True when we are in eval mode, as we must quantize
        hard = not self.training
        temp = self.temperature if temp is None else temp
        
        z_reshaped_norm = self.norm(z.view(-1, self.embed_dim))
        embedding_norm = self.norm(self.embedding.weight)

        logits = - torch.sum(z_reshaped_norm ** 2, dim=1, keepdim=True) - \
                 torch.sum(embedding_norm**2, dim=1) + 2 * \
                 torch.einsum('b d, n d -> b n', z_reshaped_norm, embedding_norm)
        logits =  logits.view(*z.shape[:2], -1)

        soft_one_hot = F.gumbel_softmax(logits, tau=temp, dim=2, hard=hard)
        z_qnorm = torch.einsum('b t n, n d -> b t d', soft_one_hot, embedding_norm)
        
        # add kl divergence to the prior loss
        qy = F.softmax(logits, dim=2)
        loss = self.kl_weight * torch.sum(qy * torch.log(qy * self.n_embed + 1e-10), dim=2).mean()

        # get encoding via argmax
        encoding_indices = soft_one_hot.argmax(dim=2)
        
        return z_qnorm, loss, encoding_indices
