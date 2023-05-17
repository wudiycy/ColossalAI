from typing import Dict, List, Tuple, Type, Any, Callable
import torch.nn as nn
from .basepolicy import Policy, Layer, Argument
import colossalai.nn as col_nn
from transformers.models.bert.modeling_bert import BertLayer, BertEmbeddings
from dataclasses import dataclass


class BertPolicy(Policy):
    @staticmethod
    def argument_policy(config, world_size: int) -> Dict[nn.Module,Argument]:
        return {
            BertLayer: Argument(
                attr_dict = {
                    # 1. shard hidden size
                    "attention.self.all_head_size": config.hidden_size // world_size,
                    "crossattention.self.all_head_size": config.hidden_size // world_size,
                    # 2. shard number of heads
                    "attention.self.num_attention_heads": config.num_attention_heads // world_size,
                    "crossattention.self.num_attention_heads": config.num_attention_heads // world_size,
       
                },
                param_funcs = [
                    BertPolicy.attn_in,
                    BertPolicy.attn_out,
                    # BertPolicy.mlp_in,
                    # BertPolicy.mlp_out
                ]
            ), 
            BertEmbeddings: Argument(
                attr_dict = {
                    # 1. shard vocab size
                    "word_embeddings.num_embeddings": config.vocab_size // world_size,
                    # 2. add the size of the sliced embedding layer excluding the last slice
                    "word_embeddings.dim_size": (config.vocab_size+world_size-1) // world_size,
                },
                param_funcs = [
                    BertPolicy.embedding,
                    BertPolicy.unembedding,
                ]
            )
        }

    @staticmethod
    def attn_in() -> List:
        return [
            Layer(
                weight="attention.self.query.weight",
                bias="attention.self.query.bias",
                replace_layer=col_nn.Linear,
            ),
            Layer(
                weight="attention.self.key.weight",
                bias="attention.self.key.bias",
                replace_layer=col_nn.Linear,
            ),
            Layer(
                weight="attention.self.value.weight",
                bias="attention.self.value.bias",
                replace_layer=col_nn.Linear,
            ),
            Layer(
                weight="crossattention.self.query.weight",
                bias="crossattention.self.query.bias",
                replace_layer=col_nn.Linear,
                ignore=True,
            ),
            Layer(
                weight="crossattention.self.key.weight",
                bias="crossattention.self.key.bias",
                replace_layer=col_nn.Linear,
                ignore=True,
            ),
            Layer(
                weight="crossattention.self.value.weight",
                bias="crossattention.self.value.bias",
                replace_layer=col_nn.Linear,
                ignore=True,
            ),

        ]
    
    @staticmethod
    def attn_out() -> List:
        return [
            Layer(
                weight="attention.output.dense.weight",
                bias="attention.output.dense.bias",
                # replace_layer=col_nn.Linear,
            ),
            Layer(
                weight="crossattention.output.dense.weight",
                bias="crossattention.output.dense.bias",
                # replace_layer=col_nn.Linear,
                ignore=True,
            ),
        ]
    
    @staticmethod
    def mlp_in() -> List:
        return [
             Layer(
                weight="intermediate.dense.weight",
                bias="intermediate.dense.bias",
                replace_layer=col_nn.Linear,
            ),
        ]
    
    @staticmethod
    def mlp_out() -> List:
        return [
             Layer(
                weight="output.dense.weight",
                bias="output.dense.bias",
                replace_layer=col_nn.Linear,
            ),
        ]

    @staticmethod
    def embedding() -> List:
        return [
            Layer(
                weight="word_embeddings.weight",
                # replace=AllReduceEmbedding,
            )
        ]
    
    @staticmethod
    def unembedding() -> List:
        return [

        ]

from transformers import BertForMaskedLM
from colossalai.shardformer.model.modeling_bert import BertForMaskedLM_
class BertForMaskedLMPolicy(BertPolicy):
    @staticmethod
    def inject_policy() -> Tuple[nn.Module, nn.Module]:
        return (BertForMaskedLM, BertForMaskedLM_)
    

    
class BertForSequenceClassificationPolicy(BertPolicy):
    @staticmethod
    def inject_policy() -> Dict:
        return {}


# model = BertForMaskedLM.from_pretrained("bert-base-uncased")
# _ = BertForMaskedLMPolicy(model)
# print(isinstance(model,list(_.inject_policy().keys())[0]))