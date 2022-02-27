#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import copy
from functools import partial

import colossalai
import pytest
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
from colossalai.logging import disable_existing_loggers
from colossalai.utils import checkpoint, free_port
from colossalai.zero.sharded_model import ShardedModel
from torch.nn.parallel import DistributedDataParallel as DDP


def checkpoint_wrapper(module, enable=True):
    if enable:
        module.forward = partial(checkpoint, module.forward)
    return module


class Net(nn.Module):
    def __init__(self, checkpoint=False) -> None:
        super().__init__()
        self.fc1 = nn.Linear(5, 5)
        self.fc2 = nn.Linear(5, 5)
        self.fc3 = nn.Linear(5, 1)
        if checkpoint:
            self.fc1 = checkpoint_wrapper(self.fc1)
        self.layers = [
            self.fc1,
            self.fc2,
            self.fc1,
            self.fc2,
            self.fc3
        ]

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return x


def run_step(model, optimizer, x, enable_autocast=False):
    model.train()
    optimizer.zero_grad()
    with torch.cuda.amp.autocast(enabled=enable_autocast):
        y = model(x)
        loss = y.sum()
    loss = loss.float()
    loss.backward()
    optimizer.step()


def allclose(tensor_a: torch.Tensor, tensor_b: torch.Tensor, loose=False) -> bool:
    if loose:
        return torch.allclose(tensor_a, tensor_b, atol=1e-3, rtol=1e-3)
    return torch.allclose(tensor_a, tensor_b)


def check_grads(model, zero_model, loose=False):
    rank = dist.get_rank()
    for p, zero_p in zip(model.parameters(), zero_model.parameters()):
        zero_grad = zero_p.grad.clone().to(p.device)
        chunks = torch.flatten(p.grad).chunk(4)
        if rank >= len(chunks):
            continue
        grad = chunks[rank]
        if zero_p.zero_shard_padding > 0:
            zero_grad = zero_grad[:-zero_p.zero_shard_padding]
        assert grad.dtype == zero_grad.dtype
        assert allclose(grad, zero_grad, loose=loose)


def check_params(model, zero_model, loose=False):
    rank = dist.get_rank()
    for p, zero_p in zip(model.parameters(), zero_model.parameters()):
        zero_shard_padding = zero_p.zero_shard_padding
        zero_p = zero_p.clone().to(p.device)
        chunks = torch.flatten(p).chunk(4)
        if rank >= len(chunks):
            continue
        p = chunks[rank]
        if zero_shard_padding > 0:
            zero_p = zero_p[:-zero_shard_padding]
        assert p.dtype == zero_p.dtype
        assert allclose(p, zero_p, loose=loose)


def decode_booleans(intval, bits):
    res = []
    for bit in range(bits):
        mask = 1 << bit
        res.append((intval & mask) == mask)
    return res


def check_config(checkpoint=False, fp16=False, offload=False):
    model = Net(checkpoint=checkpoint).cuda()
    zero_model = copy.deepcopy(model)
    ddp_model = DDP(model)

    offload_config = {}
    if offload:
        offload_config['device'] = 'cpu'
        zero_model = zero_model.cpu()
    zero_model = ShardedModel(zero_model, mixed_precision=fp16, offload_config=offload_config)

    optimizer = torch.optim.Adam(ddp_model.parameters(), lr=1e-3)
    zero_optimizer = torch.optim.Adam(zero_model.parameters(), lr=1e-3)
    for _ in range(5):
        x = torch.rand(2, 5).cuda()
        run_step(ddp_model, optimizer, x, enable_autocast=fp16)
        run_step(zero_model, zero_optimizer, x, enable_autocast=fp16)
        check_grads(ddp_model, zero_model)
        check_params(ddp_model, zero_model)
    for _ in range(5):
        x = torch.rand(2, 5).cuda()
        run_step(ddp_model, optimizer, x, enable_autocast=False)
        run_step(zero_model, zero_optimizer, x, enable_autocast=False)
        check_grads(ddp_model, zero_model, loose=True)
        check_params(ddp_model, zero_model, loose=True)


def run_dist(rank, world_size, port):
    disable_existing_loggers()
    colossalai.launch(config={},
                      rank=rank,
                      world_size=world_size,
                      host='localhost',
                      port=port,
                      backend='nccl')

    args = ['checkpoint', 'fp16', 'offload']

    def pack_args(i):
        booleans = decode_booleans(i, len(args))
        return {arg: booleans[idx] for idx, arg in enumerate(args)}

    for j in range(2 ** len(args)):
        kwargs = pack_args(j)
        if dist.get_rank() == 0:
            print(kwargs)
        check_config(**kwargs)


@pytest.mark.dist
def test_zero_level_3():
    world_size = 4
    run_func = partial(run_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_zero_level_3()
