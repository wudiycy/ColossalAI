import pytest
import torch
from torch import distributed as dist

import colossalai
from colossalai.logging import disable_existing_loggers
from colossalai.shardformer.layer.utils import Randomizer
from colossalai.tensor.d_tensor.api import clear_layout_converter
from colossalai.testing import clear_cache_before_run, parameterize, rerun_if_address_is_in_use, spawn
from tests.kit.model_zoo import model_zoo
from tests.test_shardformer.test_model._utils import (
    build_model_from_hybrid_plugin,
    check_grad,
    check_loss,
    check_output_hidden_state,
    check_weight,
    run_forward_backward_with_hybrid_plugin,
)


def check_forward_backward(model_fn, data_gen_fn, output_transform_fn, loss_fn, test_config):

    org_model, org_optimizer, sharded_model, sharded_optimizer, criterion, booster = \
        build_model_from_hybrid_plugin(model_fn, loss_fn, test_config)

    org_loss, org_output, sharded_loss, sharded_output = \
            run_forward_backward_with_hybrid_plugin(
            org_model,
            sharded_model,
            sharded_optimizer,
            data_gen_fn,
            output_transform_fn,
            criterion,
            booster)
    print(org_model.__class__.__name__)
    stage_manager = booster.plugin.stage_manager
    tp_group = booster.plugin.tp_group
    # check last hidden state & loss
    if stage_manager is None or stage_manager.is_last_stage():
        if org_model.__class__.__name__ == 'BertModel':
            check_output_hidden_state(org_output, sharded_output, stage_manager, atol=1e-5, rtol=1e-3)

        check_loss(org_loss, sharded_loss, atol=1e-5, rtol=1e-3)
    # unwrap model
    if org_model.__class__.__name__ == 'BertModel':
        bert = org_model
        sharded_bert = sharded_model.unwrap()
    else:
        bert = org_model.bert
        sharded_bert = sharded_model.unwrap().bert

    col_layer_for_check = ['encoder.layer[0].output.dense']
    row_layer_for_check = ['embeddings.word_embeddings', 'encoder.layer[0].intermediate.dense']

    if stage_manager is None or stage_manager.is_first_stage():
        #check_weight(bert.embeddings.word_embeddings, sharded_bert.embeddings.word_embeddings, tp_group, atol=1e-5, rtol=1e-3)
        #check_weight(bert.encoder.layer[0].attention.self.query, sharded_bert.encoder.layer[0].attention.self.query, tp_group, atol=5e-3, rtol=1e-3)
        check_grad(bert, sharded_bert, col_layer_for_check, tp_group, atol=1e-4, rtol=1e-3, dim=1, verbose=False)
        check_grad(bert, sharded_bert, row_layer_for_check, tp_group, atol=1e-4, rtol=1e-3, dim=0, verbose=False)

    # check weights after optimizer.step()
    org_optimizer.step()
    sharded_optimizer.step()
    if stage_manager is None or stage_manager.is_first_stage():
        check_weight(bert, sharded_bert, col_layer_for_check, tp_group, atol=5e-3, rtol=1e-3, dim=1, verbose=False)

    torch.cuda.empty_cache()


@parameterize('test_config', [{
    'tp_size': 1,
    'pp_size': 2,
    'num_microbatches': 4,
    'use_lazy_init': True
}, {
    'tp_size': 2,
    'pp_size': 2,
    'num_microbatches': 4,
    'enable_fused_normalization': False,
    'use_lazy_init': False
}, {
    'tp_size': 4,
    'pp_size': 1,
    'enable_fused_normalization': True,
    'use_lazy_init': False
}])
def run_bert_test(test_config):

    sub_model_zoo = model_zoo.get_sub_registry('transformers_bert')
    test_config['precision'] = 'float'

    for name, (model_fn, data_gen_fn, output_transform_fn, loss_fn, _) in sub_model_zoo.items():
        check_forward_backward(model_fn, data_gen_fn, output_transform_fn, loss_fn, test_config)

    clear_layout_converter()
    Randomizer.reset_index()
    torch.cuda.empty_cache()


def check_bert(rank, world_size, port):
    disable_existing_loggers()
    colossalai.launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    run_bert_test()


@pytest.mark.dist
@rerun_if_address_is_in_use()
@clear_cache_before_run()
def test_bert():
    spawn(check_bert, 4)


if __name__ == "__main__":
    test_bert()
