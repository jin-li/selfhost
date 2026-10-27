"""Private NONCOMMERCIAL S2-Pro evaluation, eager ROCm only."""
import io
import json
import os
import re
import threading
from pathlib import Path
from contextlib import asynccontextmanager, contextmanager

import numpy as np
import soundfile as sf
import torch
import torchaudio
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from huggingface_hub import snapshot_download
from huggingface_hub.constants import HF_HUB_CACHE

MODEL_REV = '1de9996b6be38b745688de084d87a5633f714e4e'
MODEL_ID = 'fishaudio/s2-pro'
MAX_CHUNK_CHARS = 200
WEIGHT_SIZES = {
    'codec.pth': 1871099728,
    'model-00001-of-00002.safetensors': 4986872984,
    'model-00002-of-00002.safetensors': 4136876104,
}
REQUIRED_FILES = set(WEIGHT_SIZES) | {
    'config.json', 'model.safetensors.index.json', 'tokenizer.json',
    'tokenizer_config.json', 'special_tokens_map.json', 'chat_template.jinja',
}
lock = threading.Lock()
ready = False


@contextmanager
def inference_slot():
    if not lock.acquire(blocking=False):
        raise HTTPException(429, 'Fish GPU is busy; retry after the active request')
    try:
        yield
    finally:
        lock.release()


def chunks(text, limit=MAX_CHUNK_CHARS):
    """Sentence-aware, lossless Unicode chunks with a strict character bound."""
    if limit < 1:
        raise ValueError('Chunk limit must be positive')
    sentences = re.findall(r'.+?(?:[。！？!?]+[”’"\']*|[.](?=\s|$)|\n+|$)', text, re.S)
    current = ''
    for sentence in sentences:
        while len(sentence) > limit:
            cut = max(sentence.rfind(mark, 0, limit) for mark in (' ', '，', ',', '；', ';', '、'))
            cut = cut + 1 if cut > limit // 2 else limit
            head, sentence = sentence[:cut], sentence[cut:]
            if current:
                yield current
                current = ''
            yield head
        if len(current) + len(sentence) > limit and current:
            yield current
            current = ''
        current += sentence
    if current:
        yield current


def local_snapshot_ready(path):
    try:
        if not all((path / name).is_file() and (path / name).stat().st_size > 0
                   for name in REQUIRED_FILES):
            return False
        if not all((path / name).stat().st_size == size for name, size in WEIGHT_SIZES.items()):
            return False
        json.loads((path / 'config.json').read_text())
        index = json.loads((path / 'model.safetensors.index.json').read_text())
        return set(index['weight_map'].values()) == set(WEIGHT_SIZES) - {'codec.pth'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def snapshot():
    local = Path(HF_HUB_CACHE) / 'models--fishaudio--s2-pro' / 'snapshots' / MODEL_REV
    if local_snapshot_ready(local):
        return Path(snapshot_download(MODEL_ID, revision=MODEL_REV, local_files_only=True))
    return Path(snapshot_download(MODEL_ID, revision=MODEL_REV, max_workers=2,
        allow_patterns=['*.json', '*.safetensors', '*.pth', '*.jinja', 'LICENSE.md', 'README.md']))


@asynccontextmanager
async def lifespan(app):
    global ready, model, decode_one_token, codec
    if os.environ.get('FISH_PRIVATE_EVALUATION') != 'noncommercial':
        raise RuntimeError('Set FISH_PRIVATE_EVALUATION=noncommercial for private evaluation only')
    assert torch.cuda.is_available() and torch.version.hip, 'ROCm GPU required; CPU fallback refused'
    torch.cuda.set_device(0)
    # Force safe eager attention, including calls outside explicit SDPA contexts.
    torch.backends.cuda.enable_flash_sdp(False)
    torch.backends.cuda.enable_mem_efficient_sdp(False)
    torch.backends.cuda.enable_math_sdp(True)
    probe = torch.randn(32, 32, device='cuda', dtype=torch.bfloat16)
    assert torch.isfinite(probe @ probe).all()
    checkpoint = snapshot()
    from fish_speech.models.text2semantic.inference import init_model
    from fish_speech.models.dac.inference import load_model
    model, decode_one_token = init_model(checkpoint, 'cuda', torch.bfloat16, compile=False)
    assert model.tokenizer is not None and model.tokenizer.semantic_begin_id > 0, 'S2 semantic tokenizer failed to load'
    with torch.device('cuda'):
        model.setup_caches(max_batch_size=1, max_seq_len=model.config.max_seq_len,
                           dtype=next(model.parameters()).dtype)
    codec = load_model('modded_dac_vq', checkpoint / 'codec.pth', 'cuda')
    assert next(model.parameters()).is_cuda and next(codec.parameters()).is_cuda, 'Both models must be on GPU'
    ready = True
    yield
    ready = False


app = FastAPI(lifespan=lifespan)


class Speech(BaseModel):
    input: str = Field(min_length=1, max_length=4000)
    model: str = 's2-pro'
    voice: str = 'default-en'
    response_format: str = 'wav'
    speed: float = 1.0
    seed: int = 42
    language: str | None = None


@app.get('/health')
def health():
    if not ready:
        raise HTTPException(503, 'loading')
    return {'loaded': True, 'model': MODEL_ID, 'revision': MODEL_REV,
            'backend': 'rocm', 'hip': torch.version.hip,
            'torch': torch.__version__, 'device': torch.cuda.get_device_name(0),
            'model_device': str(next(model.parameters()).device),
            'codec_device': str(next(codec.parameters()).device),
            'compile': False, 'chunk_chars': MAX_CHUNK_CHARS, 'license_scope': 'private noncommercial evaluation only'}


@app.post('/v1/audio/speech')
def speech(req: Speech):
    if req.model not in ('s2-pro', MODEL_ID) or req.response_format != 'wav' or req.speed != 1:
        raise HTTPException(400, 'Supported: s2-pro, wav, speed=1')
    voice = {'default-en': 'ryan-en', 'default-zh': 'uncle-fu-zh'}.get(req.voice, req.voice)
    if voice not in ('ryan-en', 'uncle-fu-zh'):
        raise HTTPException(400, 'Unknown synthetic reference voice')
    if req.language not in (None, 'en', 'zh'):
        raise HTTPException(400, 'Supported language hints: en, zh; pronunciation is text-driven')
    if not req.input.strip():
        raise HTTPException(422, 'Input must contain narration text')
    from fish_speech.models.text2semantic.inference import generate_long
    reference = Path('/voices') / voice
    if not reference.with_suffix('.wav').is_file() or not reference.with_suffix('.txt').is_file():
        raise HTTPException(503, 'Reference voice is missing')
    transcript = reference.with_suffix('.txt').read_text().strip()
    if not transcript:
        raise HTTPException(422, 'Reference transcript is empty')
    try:
        info = sf.info(reference.with_suffix('.wav'))
        if not 1 <= info.duration <= 30 or not 8000 <= info.samplerate <= 192000:
            raise HTTPException(422, 'Reference WAV must be 1–30 seconds, 8–192 kHz')
        audio, sr = sf.read(reference.with_suffix('.wav'), dtype='float32', always_2d=True)
    except (RuntimeError, ValueError) as error:
        raise HTTPException(422, 'Invalid reference WAV') from error
    if not np.isfinite(audio).all():
        raise HTTPException(422, 'Reference WAV contains nonfinite samples')
    with inference_slot(), torch.inference_mode():
        torch.manual_seed(req.seed)
        audio = torch.from_numpy(audio.mean(axis=1)).to('cuda')[None, None]
        audio = torchaudio.functional.resample(audio, sr, codec.sample_rate)
        lengths = torch.tensor([audio.shape[-1]], device='cuda', dtype=torch.long)
        prompt_tokens = codec.encode(audio, lengths)[0][0]
        segments = []
        for text_chunk in chunks(req.input):
            if not text_chunk.strip():
                continue
            for result in generate_long(model=model, device='cuda', decode_one_token=decode_one_token,
                    text=text_chunk, num_samples=1, max_new_tokens=1024, top_p=0.7,
                    repetition_penalty=1.2, temperature=0.7, compile=False,
                    iterative_prompt=True, chunk_length=MAX_CHUNK_CHARS,
                    prompt_text=[transcript], prompt_tokens=[prompt_tokens]):
                if result.action == 'sample':
                    # Match upstream: decode one emitted sample, retain only CPU PCM.
                    with torch.autocast('cuda', dtype=torch.bfloat16):
                        segment = codec.from_indices(result.codes[None])[0, 0].float().cpu().numpy()
                    segments.append(segment)
        if not segments:
            raise HTTPException(500, 'No audio tokens generated')
        samples = np.concatenate(segments)
        if not np.isfinite(samples).all() or np.max(np.abs(samples)) < 1e-5:
            raise HTTPException(500, 'Nonfinite or silent generated audio')
        output = io.BytesIO()
        sf.write(output, samples, codec.sample_rate, format='WAV', subtype='PCM_16')
        return Response(output.getvalue(), media_type='audio/wav')


if __name__ == '__main__':
    if os.environ.get('FISH_PREFETCH_ONLY') == '1':
        print(snapshot())
    else:
        import uvicorn
        uvicorn.run(app, host='0.0.0.0', port=8008)
