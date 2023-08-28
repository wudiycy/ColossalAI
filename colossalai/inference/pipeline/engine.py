import re
from functools import partial
from types import MethodType
from typing import Callable, List, Optional, Set

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn

from colossalai.cluster import ProcessGroupMesh
from colossalai.pipeline.schedule.generate import GenerateSchedule
from colossalai.pipeline.stage_manager import PipelineStageManager
from colossalai.shardformer import ShardConfig, ShardFormer
from colossalai.shardformer._utils import getattr_
from colossalai.shardformer.policies.base_policy import Policy

from .microbatch_manager import MicroBatchManager
from .policy.gpt2_ppinfer import GPT2LMHeadModelPipelinePolicy
from .utils import get_suffix_name, set_tensors_to_none


class PPInferEngine:
    '''
    PPInferEngine is a class that handles the pipeline parallel inference.

    Args:
        pp_size (int): the number of pipeline stages.
        pp_model (`nn.Module`): the model already in pipeline parallelism style.
        model (`nn.Module`): the model not in pipeline style, and will be modified with `ShardFormer`.
        model_policy (`colossalai.shardformer.policies.base_policy.Policy`): the policy to shardformer model.
        micro_batch_size (int): the micro batch size.
        micro_batch_buffer_size (int): the buffer size for micro batch. Normally, it should be the same as the number of pipeline stages.
        new_length (int): the new length of the input sequence.
        early_stopping (bool): whether to stop early.

    Example:

    ```python
    from colossalai.ppinference import PPInferEngine
    from transformers import GPT2LMHeadModel, GPT2Tokenizer

    model = transformers.GPT2LMHeadModel.from_pretrained('gpt2')
    # assume the model is infered with 4 pipeline stages
    inferengine = PPInferEngine(pp_size=4, model=model, model_policy={Your own policy for pipeline sharding})

    input = ["Hello, my dog is cute, and I like"]
    tokenized_input = tokenizer(input, return_tensors='pt')
    output = engine.inference([tokenized_input])
    ```

    '''

    def __init__(
        self,
        pp_size: int,
        pp_model: nn.Module = None,
        model: nn.Module = None,
        model_policy: Policy = None,
        new_length: int = 32,
        micro_batch_size: int = 1,
        micro_batch_buffer_size: int = None,
    # TODO: implement early_stopping, and various gerneration options
        early_stopping: bool = False,
        do_sample: bool = False,
        num_beams: int = 1,
    ) -> None:
        assert pp_model or (model and model_policy), "Either pp_model or model with model_policy should be provided."
        self.pp_size = pp_size
        self.pg_mesh = ProcessGroupMesh(pp_size)
        self.stage_manager = PipelineStageManager(self.pg_mesh, 0, True)
        self.mb_manager = MicroBatchManager(new_length, micro_batch_size, micro_batch_buffer_size or pp_size)
        self.schedule = GenerateSchedule(self.stage_manager, self.mb_manager)
        self.model = pp_model or self._shardformer(model, model_policy)

    def inference(self, input_list):
        out = self.schedule.generate_step(self.model, iter(input_list))
        return out

    def _shardformer(self, model, model_policy):
        shardconfig = ShardConfig(
            tensor_parallel_process_group=None,
            pipeline_stage_manager=self.stage_manager,
            enable_tensor_parallelism=False,
            enable_fused_normalization=False,
            enable_all_optimization=False,
            enable_flash_attention=False,
            enable_jit_fused=False,
            enable_sequence_parallelism=False,
        )
        shardformer = ShardFormer(shard_config=shardconfig)
        shard_model, _ = shardformer.optimize(model, model_policy)
        return shard_model.cuda()
