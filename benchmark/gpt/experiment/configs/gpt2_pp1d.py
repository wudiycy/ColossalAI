from model import GPT2_exlarge_pipeline_1D
from torch.optim import Adam
from colossalai.amp import AMP_TYPE
import torch
from model import vocab_parallel_cross_entropy

BATCH_SIZE = 128
NUM_EPOCHS = 60
SEQ_LEN = 1024
NUM_MICRO_BATCHES = 16
TENSOR_SHAPE = (BATCH_SIZE // NUM_MICRO_BATCHES, SEQ_LEN, 1600)

fp16 = dict(
    mode=AMP_TYPE.NAIVE
)

parallel = dict(
    pipeline=2,
    tensor=dict(mode='1d', size=4)
)

optimizer = dict(
    type=Adam,
    lr=0.00015,
    weight_decay=1e-2,
)

model = dict(
    type=GPT2_exlarge_pipeline_1D,
    checkpoint=True,
    dtype=torch.half,
    # embed_split_hidden=True,
    # num_chunks=2,
)

loss_fn = dict(type=vocab_parallel_cross_entropy)
