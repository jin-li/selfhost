"""Verify the pinned S2 codec on ROCm, independently of semantic-model loading."""
import os
import time
from pathlib import Path
import numpy as np
import soundfile as sf
import torch
import torchaudio
from fish_speech.models.dac.inference import load_model

assert torch.cuda.is_available() and torch.version.hip, 'ROCm GPU required'
torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)
torch.backends.cuda.enable_math_sdp(True)
checkpoint = Path(os.environ['FISH_CODEC_PATH'])
reference = Path(os.environ.get('FISH_REFERENCE_PATH', '/voices/ryan-en.wav'))
audio, rate = sf.read(reference, dtype='float32', always_2d=True)
start = time.monotonic()
with torch.inference_mode():
    probe = torch.randn(32, 32, device='cuda', dtype=torch.bfloat16)
    assert torch.isfinite(probe @ probe).all()
    model = load_model('modded_dac_vq', checkpoint, 'cuda')
    samples = torch.from_numpy(audio.mean(axis=1)).to('cuda')[None, None]
    samples = torchaudio.functional.resample(samples, rate, model.sample_rate)
    lengths = torch.tensor([samples.shape[-1]], device='cuda', dtype=torch.long)
    codes, _ = model.encode(samples, lengths)
    with torch.autocast('cuda', dtype=torch.bfloat16):
        decoded = model.from_indices(codes)[0, 0]
        output = decoded.float().cpu().numpy()
    assert np.isfinite(output).all() and np.max(np.abs(output)) > 1e-5
    assert abs(len(output) / model.sample_rate - len(audio) / rate) < 0.2
    sf.write(os.environ.get('FISH_CODEC_OUTPUT', '/tmp/fish-codec-reconstruction.wav'),
             output, model.sample_rate, subtype='PCM_16')
print({'backend': 'rocm', 'hip': torch.version.hip, 'torch': torch.__version__,
       'device': torch.cuda.get_device_name(0), 'codec_device': str(next(model.parameters()).device),
       'codec_weights_dtype': str(next(model.parameters()).dtype),
       'decoded_dtype': str(decoded.dtype), 'sample_rate': model.sample_rate, 'duration': len(output) / model.sample_rate,
       'elapsed': time.monotonic() - start, 'codes_shape': list(codes.shape)})
