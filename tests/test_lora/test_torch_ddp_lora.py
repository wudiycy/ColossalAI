import copy
import os

import torch
from torch import distributed as dist
from torch.optim import AdamW

import colossalai
from colossalai.booster import Booster
from colossalai.booster.plugin import TorchDDPPlugin
from colossalai.testing import (
    assert_equal,
    assert_not_equal,
    check_state_dict_equal,
    clear_cache_before_run,
    rerun_if_address_is_in_use,
    spawn,
)
from tests.kit.model_zoo import model_zoo
from tests.test_checkpoint_io.utils import shared_tempdir


@clear_cache_before_run()
def check_fwd_bwd(model_fn, data_gen_fn, output_transform_fn, loss_fn, task_type):
    model = model_fn()

    plugin = TorchDDPPlugin()
    booster = Booster(plugin=plugin)
    model = booster.enable_lora(model, task_type=task_type, r=8, lora_alpha=32, lora_dropout=0.1)
    model_copy = copy.deepcopy(model)

    optimizer = AdamW(model.parameters(), lr=0.001)
    criterion = loss_fn

    model, optimizer, criterion, _, _ = booster.boost(model, optimizer, criterion)

    data = data_gen_fn()
    data = {k: v.to("cuda") if torch.is_tensor(v) or "Tensor" in v.__class__.__name__ else v for k, v in data.items()}

    output = model(**data)
    output = output_transform_fn(output)
    loss = criterion(output)

    booster.backward(loss, optimizer)
    optimizer.clip_grad_by_norm(1.0)
    optimizer.step()

    for (n1, p1), (n2, p2) in zip(model.named_parameters(), model_copy.named_parameters()):
        if "lora_" in n1:
            # lora modules require gradients, thus updated
            assert p1.requires_grad
            assert_not_equal(p1.to(p2.device), p2)
        else:
            if not p1.requires_grad:
                assert_equal(p1.to(p2.device), p2)


@clear_cache_before_run()
def check_checkpoint(model_fn, data_gen_fn, output_transform_fn, loss_fn, task_type):
    plugin = TorchDDPPlugin()

    model_save = model_fn()
    model_load = copy.deepcopy(model_save)

    booster = Booster(plugin=plugin)
    model_save = booster.enable_lora(model_save, task_type=task_type, r=8, lora_alpha=32, lora_dropout=0.1)
    model_save, _, _, _, _ = booster.boost(model_save)

    with shared_tempdir() as tempdir:
        lora_ckpt_path = os.path.join(tempdir, "ckpt")
        booster.save_lora_as_pretrained(model_save, lora_ckpt_path)
        dist.barrier()

        # The Lora checkpoint should be small in size
        checkpoint_size_mb = os.path.getsize(os.path.join(lora_ckpt_path, "adapter_model.bin")) / (1024 * 1024)
        assert checkpoint_size_mb < 1

        model_load = booster.enable_lora(model_load, pretrained_dir=lora_ckpt_path)
        model_load, _, _, _, _ = booster.boost(model_load)

        check_state_dict_equal(model_save.state_dict(), model_load.state_dict())


def run_lora_test():
    sub_model_zoo = model_zoo.get_sub_registry("transformers_llama")
    for name, (model_fn, data_gen_fn, output_transform_fn, loss_fn, _) in sub_model_zoo.items():
        task_type = None
        if name == "transformers_llama_for_casual_lm":
            task_type = "CAUSAL_LM"
        if name == "transformers_llama_for_sequence_classification":
            task_type = "SEQ_CLS"
        check_fwd_bwd(model_fn, data_gen_fn, output_transform_fn, loss_fn, task_type)
        check_checkpoint(model_fn, data_gen_fn, output_transform_fn, loss_fn, task_type)


def run_dist(rank, world_size, port):
    config = {}
    colossalai.launch(config=config, rank=rank, world_size=world_size, host="localhost", port=port, backend="nccl")
    run_lora_test()


@rerun_if_address_is_in_use()
def test_torch_ddp_lora():
    spawn(run_dist, 2)
