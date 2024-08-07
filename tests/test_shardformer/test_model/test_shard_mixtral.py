import os
import shutil
from copy import deepcopy
from typing import Tuple

import pytest
import torch
import torch.distributed
import torch.distributed as dist
from transformers.models.mixtral.configuration_mixtral import MixtralConfig
from transformers.models.mixtral.modeling_mixtral import MixtralModel

import colossalai
from colossalai.booster.booster import Booster
from colossalai.booster.plugin.moe_hybrid_parallel_plugin import MoeHybridParallelPlugin
from colossalai.testing import parameterize, rerun_if_address_is_in_use, spawn
from colossalai.testing.random import seed_all
from tests.test_moe.moe_utils import assert_loose_close, check_model_equal

NUM_BATCH = 8
NUM_TOK_PER_BATCH, NUM_EXPERTS = 4, 4
NUM_LAYERS = 4
HIDDEN_SIZE_PER_HEAD = 4
NUM_HEADS = 4
TOP_K = 1

CHECKED_CONFIG = [  # FOR WORLD=4
    (0, 1, 4, 1, 1),
    (0, 1, 1, 4, 1),
    (0, 1, 1, 1, 4),
    (1, 4, 1, 1, 1),
    (1, 1, 4, 1, 1),
    (1, 1, 1, 4, 1),
    (1, 1, 1, 1, 4),
    (1, 2, 1, 1, 1),
]


@parameterize(
    "config",
    [
        (1, 2, 2, 1, 1),
        (1, 2, 1, 2, 1),
        (1, 2, 1, 1, 2),
    ],
)
def run_zero_with_original_model(config: Tuple[int, ...]):
    stage, ep_size, pp_size, tp_size, sp_size = config
    world_size = dist.get_world_size()
    rank = dist.get_rank()
    dtype, precision = torch.float16, "fp16"
    torch.cuda.set_device(dist.get_rank())

    plugin = MoeHybridParallelPlugin(
        pp_size=pp_size,
        num_microbatches=pp_size,
        tp_size=tp_size,
        sp_size=sp_size,
        ep_size=ep_size,
        zero_stage=stage,
        enable_sequence_parallelism=sp_size > 1,
        sequence_parallelism_mode="all_to_all" if sp_size > 1 else None,
        overlap_communication=False,
        initial_scale=1,
        precision=precision,
        find_unused_parameters=True,
    )
    dp_size = plugin.dp_size

    booster = Booster(plugin=plugin)

    assert pp_size <= NUM_LAYERS, "pp_size should be less than or equal to NUM_LAYERS"
    config = MixtralConfig(
        hidden_size=HIDDEN_SIZE_PER_HEAD * NUM_HEADS,
        intermediate_size=HIDDEN_SIZE_PER_HEAD * NUM_HEADS * 2,
        num_hidden_layers=NUM_LAYERS,
        num_attention_heads=NUM_HEADS,
        num_key_value_heads=NUM_HEADS,
        num_local_experts=NUM_EXPERTS,
        num_experts_per_tok=TOP_K,
        attn_implementation="flash_attention_2",
    )

    # init model with the same seed
    seed_all(10086)

    torch_model = MixtralModel(config).to(dtype).cuda()
    torch_optimizer = torch.optim.SGD(torch_model.parameters(), lr=1)

    parallel_model = deepcopy(torch_model)
    parallel_optimizer = torch.optim.SGD(parallel_model.parameters(), lr=1)
    parallel_model, parallel_optimizer, _, _, _ = booster.boost(parallel_model, parallel_optimizer)

    # create different input along dp axis
    seed_all(1453 + rank)

    torch_model.train()
    parallel_model.train()
    for _ in range(2):
        # gen random input
        input_embeddings = torch.rand(
            NUM_BATCH, NUM_TOK_PER_BATCH, HIDDEN_SIZE_PER_HEAD * NUM_HEADS, requires_grad=True
        ).cuda()
        dist.all_reduce(
            input_embeddings, group=plugin.pp_group
        )  # pp inputs except the first stage doesn't matter, but need to be replicate for torch model check

        dist.all_reduce(input_embeddings, group=plugin.tp_group)  # tp group duplicate input
        dist.all_reduce(input_embeddings, group=plugin.sp_group)  # sp group duplicate input

        # run the model with hybrid parallel
        if booster.plugin.stage_manager is not None:
            # for test with pp
            data_iter = iter([{"inputs_embeds": input_embeddings}])
            sharded_output = booster.execute_pipeline(
                data_iter,
                parallel_model,
                lambda x, y: x.last_hidden_state.mean(),
                parallel_optimizer,
                return_loss=True,
                return_outputs=True,
            )
            if booster.plugin.stage_manager.is_last_stage():
                parallel_output = sharded_output["loss"]
            else:
                parallel_output = torch.tensor(12345.0, device="cuda")

            # broadcast along pp axis
            dist.broadcast(
                parallel_output, src=dist.get_process_group_ranks(plugin.pp_group)[-1], group=plugin.pp_group
            )
        else:
            # for test without pp
            parallel_output = parallel_model(inputs_embeds=input_embeddings.to(dtype)).last_hidden_state.mean()
            parallel_optimizer.backward(parallel_output)
        parallel_optimizer.step()
        parallel_optimizer.zero_grad()
        dist.all_reduce(parallel_output, group=plugin.dp_group)

        # ===================================================================================
        # run normal model with all dp(different) inputs
        all_inputs = [torch.empty_like(input_embeddings) for _ in range(dp_size)]
        dist.all_gather(all_inputs, input_embeddings, group=plugin.dp_group)
        torch_output_sum = 0
        for input_data_ in all_inputs:
            torch_output = torch_model(inputs_embeds=input_data_.to(dtype)).last_hidden_state.mean()
            torch_output.backward()
            torch_output_sum += torch_output.detach()
        # avg dp grads follows zero optimizer
        for p in torch_model.parameters():
            if p.grad is not None:
                p.grad /= dp_size
        torch_optimizer.step()
        torch_optimizer.zero_grad()

        print("parallel_output", parallel_output.dtype, dtype)
        assert_loose_close(parallel_output, torch_output_sum, dtype=dtype)

    # use checkpoint to load sharded zero model
    model_dir = "./test_mixtral"
    if rank == world_size - 1:
        os.makedirs(model_dir, exist_ok=True)

    dist.barrier()
    booster.save_model(parallel_model, model_dir, shard=True)
    dist.barrier()

    saved_model = MixtralModel.from_pretrained(model_dir).cuda().to(dtype)
    check_model_equal(torch_model, saved_model)
    dist.barrier()

    if rank == world_size - 1:
        shutil.rmtree(model_dir)

    print(f"rank {dist.get_rank()} test passed")


def run_dist(rank, world_size, port):
    colossalai.launch(rank=rank, world_size=world_size, host="localhost", port=port, backend="nccl")
    run_zero_with_original_model()


@pytest.mark.dist
@pytest.mark.parametrize("world_size", [4])
@rerun_if_address_is_in_use()
def test_mixtral(world_size):
    spawn(run_dist, world_size)


if __name__ == "__main__":
    test_mixtral(world_size=4)
