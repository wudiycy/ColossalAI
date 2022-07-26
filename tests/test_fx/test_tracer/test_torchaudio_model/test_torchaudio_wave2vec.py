import torch
from torchaudio.models.wav2vec2 import (
    hubert_base,
    hubert_large,
    hubert_xlarge,
    wav2vec2_base,
    wav2vec2_large,
    wav2vec2_large_lv60k,
)
from utils import trace_and_compare

MODEL_LIST = [
    hubert_base,
    hubert_large,
    hubert_xlarge,
    # wav2vec2_base,
    # wav2vec2_large,
    # wav2vec2_large_lv60k,
]

def _smoke_test(model, device):
    model = model.to(device=device)

    batch_size, num_frames = 3, 1024
    
    def data_gen():
        waveforms = torch.randn(batch_size, num_frames, device=device)
        lengths = torch.randint(
            low=0,
            high=num_frames,
            size=[
                batch_size,
            ],
            device=device,
        )
        return dict(waveforms=waveforms, lengths=lengths)

    trace_and_compare(model, data_gen, need_meta=True, need_concrete=False)
    
def test_wav2vec():
    for model_fn in MODEL_LIST:
        _smoke_test(model_fn(), 'cuda')
    
    
if __name__ == "__main__":
    test_wav2vec()