import torch.nn as nn
from transformers.models.bert.modeling_bert import BertEmbeddings, BertLayer, BertLMPredictionHead

import colossalai.shardformer.layer as col_nn
from colossalai.shardformer.layer.dropout import Dropout1D

from ..utils import getattr_, setattr_
from .basepolicy import ModulePolicyDescription, Policy, SubModuleReplacementDescription


class BertPolicy(Policy):

    def preprocess(self):
        # reshape the embedding layer
        r"""
        Reshape the Embedding layer to make the embedding dimension divisible by world_size
        """
        # TODO:
        vocab_size = self.model.config.vocab_size
        world_size = self.shard_config.tensor_parallel_size
        if vocab_size % world_size != 0:
            new_vocab_size = vocab_size + world_size - vocab_size % world_size
            self.model.resize_token_embeddings(new_vocab_size)
        return self.model

    def module_policy(self):
        return {
            BertLayer:
                ModulePolicyDescription(
                    attribute_replacement={
        # 1. shard hidden size
                        "attention.self.all_head_size":
                            self.model.config.hidden_size // self.shard_config.tensor_parallel_size,
                        "crossattention.self.all_head_size":
                            self.model.config.hidden_size // self.shard_config.tensor_parallel_size,
        # 2. shard number of heads
                        "attention.self.num_attention_heads":
                            self.model.config.num_attention_heads // self.shard_config.tensor_parallel_size,
                        "crossattention.self.num_attention_heads":
                            self.model.config.num_attention_heads // self.shard_config.tensor_parallel_size,
                    },
                    param_replacement=[],
                    sub_module_replacement=[
                        SubModuleReplacementDescription(
                            suffix="attention.self.query",
                            target_module=col_nn.Linear1D_Col,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.self.key",
                            target_module=col_nn.Linear1D_Col,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.self.value",
                            target_module=col_nn.Linear1D_Col,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.output.dense",
                            target_module=col_nn.Linear1D_Row,
                        ),
                        SubModuleReplacementDescription(
                            suffix="intermediate.dense",
                            target_module=col_nn.Linear1D_Col,
                        ),
                        SubModuleReplacementDescription(
                            suffix="output.dense",
                            target_module=col_nn.Linear1D_Row,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.self.dropout",
                            target_module=Dropout1D,
                        ),
                        SubModuleReplacementDescription(
                            suffix="attention.output.dropout",
                            target_module=Dropout1D,
                        )
                    ]),
            BertEmbeddings:
                ModulePolicyDescription(attribute_replacement={},
                                        param_replacement=[],
                                        sub_module_replacement=[
                                            SubModuleReplacementDescription(
                                                suffix="word_embeddings",
                                                target_module=col_nn.VocabParallelEmbedding1D,
                                            )
                                        ])
        }

    def new_model_class(self):
        # do nothing
        return self.model

    def postprocess(self):
        return self.model


# BertModel
class BertModelPolicy(BertPolicy):

    def __init__(self) -> None:
        super().__init__()


# BertForPreTraining
class BertForPretrainingPolicy(BertPolicy):

    def __init__(self) -> None:
        super().__init__()

    def module_policy(self):
        module_policy = super().module_policy()
        addon_module = {
            BertLMPredictionHead:
                ModulePolicyDescription(attribute_replacement={},
                                        param_replacement=[],
                                        sub_module_replacement=[
                                            SubModuleReplacementDescription(suffix="decoder",
                                                                            target_module=col_nn.Linear1D_Col,
                                                                            kwargs={"gather_output": True})
                                        ])
        }
        module_policy.update(addon_module)
        return module_policy

    def postprocess(self):
        binding_map = {"bert.embeddings.word_embeddings.weight": "cls.predictions.decoder.weight"}
        for k, v in binding_map.items():
            param = getattr_(self.model, k)
            param = nn.Parameter(param)
            setattr_(self.model, k, param)
            setattr_(self.model, v, param)
        return self.model


# BertLMHeadModel
class BertLMHeadModelPolicy(BertPolicy):

    def __init__(self) -> None:
        super().__init__()

    def module_policy(self):
        module_policy = super().module_policy()
        addon_module = {
            BertLMPredictionHead:
                ModulePolicyDescription(attribute_replacement={},
                                        param_replacement=[],
                                        sub_module_replacement=[
                                            SubModuleReplacementDescription(suffix="decoder",
                                                                            target_module=col_nn.Linear1D_Col,
                                                                            kwargs={"gather_output": True})
                                        ])
        }
        module_policy.update(addon_module)
        return module_policy

    def postprocess(self):
        binding_map = {"bert.embeddings.word_embeddings.weight": "cls.predictions.decoder.weight"}
        for k, v in binding_map.items():
            param = getattr_(self.model, k)
            param = nn.Parameter(param)
            setattr_(self.model, k, param)
            setattr_(self.model, v, param)
        return self.model


# BertForMaskedLM
class BertForMaskedLMPolicy(BertPolicy):

    def __init__(self) -> None:
        super().__init__()

    def module_policy(self):
        module_policy = super().module_policy()
        addon_module = {
            BertLMPredictionHead:
                ModulePolicyDescription(attribute_replacement={},
                                        param_replacement=[],
                                        sub_module_replacement=[
                                            SubModuleReplacementDescription(suffix="decoder",
                                                                            target_module=col_nn.Linear1D_Col,
                                                                            kwargs={"gather_output": True})
                                        ])
        }
        module_policy.update(addon_module)
        return module_policy

    def postprocess(self):
        binding_map = {"bert.embeddings.word_embeddings.weight": "cls.predictions.decoder.weight"}
        for k, v in binding_map.items():
            param = getattr_(self.model, k)
            param = nn.Parameter(param)
            setattr_(self.model, k, param)
            setattr_(self.model, v, param)
        return self.model


# BertForSequenceClassification
class BertForSequenceClassificationPolicy(BertPolicy):

    def __init__(self) -> None:
        super().__init__()


# BertForTokenClassification
class BertForTokenClassificationPolicy(BertPolicy):

    def __init__(self) -> None:
        super().__init__()


# BertForNextSentencePrediction
class BertForNextSentencePredictionPolicy(BertPolicy):

    def __init__(self) -> None:
        super().__init__()


# BertForMultipleChoice
class BertForMultipleChoicePolicy(BertPolicy):

    def __init__(self) -> None:
        super().__init__()
