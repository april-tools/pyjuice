from __future__ import annotations

import torch
import numpy as np
import networkx as nx
from typing import Type
from pyjuice.graph import *
from pyjuice.layer import *
from typing import Optional
from pyjuice.model import ProbCircuit
from pyjuice.structures import BayesianTreeToHiddenRegionGraph


def mutual_information(x1: torch.Tensor, x2: torch.Tensor, num_bins: int, sigma: float):
    assert x1.device == x2.device

    device = x1.device
    B, K1 = x1.size()
    K2 = x2.size(1)

    x1 = (x1 - torch.min(x1)) / (torch.max(x1) - torch.min(x1) + 1e-8)
    x2 = (x2 - torch.min(x2)) / (torch.max(x2) - torch.min(x2) + 1e-8)

    bins = torch.linspace(0, 1, num_bins, device = device)

    x1p = torch.exp(-0.5 * (x1.unsqueeze(2) - bins.view(1, 1, -1)).pow(2) / sigma**2) # (B, K1, n_bin)
    x2p = torch.exp(-0.5 * (x2.unsqueeze(2) - bins.view(1, 1, -1)).pow(2) / sigma**2) # (B, K2, n_bin)

    x12p = torch.einsum("bia,baj->ij", x1p.reshape(B, K1 * num_bins, 1), x2p.reshape(B, 1, K2 * num_bins)).reshape(K1, num_bins, K2, num_bins) / B

    x1p_norm = (x1p / x1p.sum(dim = 2, keepdim = True)).mean(dim = 0)
    x2p_norm = (x2p / x2p.sum(dim = 2, keepdim = True)).mean(dim = 0)
    x12p_norm = x12p / x12p.sum(dim = (1, 3), keepdim = True) # (K1, n_bin, K2, n_bin)

    m1 = -(x1p_norm * torch.log(x1p_norm + 1e-4)).sum(dim = 1)
    m2 = -(x2p_norm * torch.log(x2p_norm + 1e-4)).sum(dim = 1)
    m12 = -(x12p_norm * torch.log(x12p_norm + 1e-4)).sum(dim = (1, 3))

    mi = m1.unsqueeze(1) + m2.unsqueeze(0) - m12
    return mi


def mutual_information_chunked(x1: torch.Tensor, x2: torch.Tensor, num_bins: int, sigma: float, chunk_size: int):
    K = x1.size(1)
    mi = torch.zeros([K, K])
    for x_s in range(0, K, chunk_size):
        x_e = min(x_s + chunk_size, K)
        for y_s in range(0, K, chunk_size):
            y_e = min(y_s + chunk_size, K)

            mi[x_s:x_e,y_s:y_e] = mutual_information(x1[:,x_s:x_e], x2[:,y_s:y_e], num_bins, sigma)

    return mi


def chow_liu_tree(mi: np.ndarray):
    K = mi.shape[0]
    G = nx.Graph()
    for v in range(K):
        G.add_node(v)
        for u in range(v):
            G.add_edge(u, v, weight = -mi[u, v])

    T = nx.minimum_spanning_tree(G)

    return T
    

def HCLT(x: torch.Tensor, num_bins: int, sigma: float,                                            
                                            chunk_size: int,
                                            num_latents: int, 
                                            max_npartitions: Optional[int] = None,
                                            input_layer_type: Type[InputLayer] = CategoricalLayer, 
                                            input_layer_params: dict = {"num_cats": 256}) -> ProbCircuit:
    
    mi = mutual_information_chunked(x, x, num_bins, sigma, chunk_size = chunk_size).detach().cpu().numpy()
    T = chow_liu_tree(mi)
    root = nx.center(T)[0]
    root_r = BayesianTreeToHiddenRegionGraph(T, root, num_latents, input_layer_type, input_layer_params)
    pc = ProbCircuit(root_r, max_npartitions=max_npartitions)
    return pc