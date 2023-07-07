import copy

from colossalai.pipeline.stage_manager import PipelineStageManager
from colossalai.shardformer import ShardConfig, ShardFormer


def build_model(model_fn, enable_fused_normalization=True, enable_tensor_parallelism=True):
    # create new model
    org_model = model_fn().cuda()

    # shard model
    shard_config = ShardConfig(enable_fused_normalization=enable_fused_normalization,
                               enable_tensor_parallelism=enable_tensor_parallelism)
    model_copy = copy.deepcopy(org_model)
    shard_former = ShardFormer(shard_config=shard_config)
    sharded_model, shared_params = shard_former.optimize(model_copy)
    return org_model, sharded_model.cuda()


def build_pipeline_model(model_fn,
                         stage_manager=None,
                         enable_fused_normalization=False,
                         enable_tensor_parallelism=False):
    # create new model
    org_model = model_fn().cuda()

    # shard model
    shard_config = ShardConfig(enable_fused_normalization=enable_fused_normalization,
                               enable_tensor_parallelism=enable_tensor_parallelism,
                               pipeline_stage_manager=stage_manager)

    model_copy = copy.deepcopy(org_model)
    shard_former = ShardFormer(shard_config=shard_config)
    sharded_model, shared_params = shard_former.optimize(model_copy)
    return org_model, sharded_model.cuda()


def run_forward(original_model, sharded_model, data_gen_fn, output_transform_fn, loss_fn):
    # prepare input
    data = data_gen_fn()
    data = {k: v.cuda() for k, v in data.items()}

    # switch to train mode
    original_model.train()
    sharded_model.train()
    # run forward
    org_output = original_model(**data)
    org_output = output_transform_fn(org_output)
    org_loss = loss_fn(org_output)

    shard_output = sharded_model(**data)
    shard_output = output_transform_fn(shard_output)
    shard_loss = loss_fn(shard_output)
    return org_output, org_loss, shard_output, shard_loss
