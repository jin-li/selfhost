"""Private, serialized OpenAI speech adapter for pinned CosyVoice3."""
import gc
import io
import logging
import os
from pathlib import Path
import re
import threading
import traceback
from contextlib import asynccontextmanager
from typing import Literal

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

VOICE_ROOT = Path('/cache/voices')
LANGUAGES = {'zh', 'en', 'ja', 'ko', 'de', 'es', 'fr', 'it', 'ru', 'auto'}
model = None
loaded_sample_rate = None
server_ready = False
lock = threading.Lock()


def chunks(text, limit=240):
    """Keep complete sentences together when possible; bound unpunctuated input."""
    sentences = re.findall(r'.+?(?:[。！？!?]+[”’"\']*|[.](?=\s|$)|\n+|$)', text, re.S)
    current = ''
    for sentence in sentences:
        while len(sentence) > limit:
            cut = max(sentence.rfind(mark, 0, limit + 1) for mark in (' ', '，', ',', '；', ';', '、'))
            cut = cut + 1 if cut > limit // 2 else limit
            head, sentence = sentence[:cut], sentence[cut:]
            if current.strip():
                yield current.strip()
                current = ''
            if head.strip():
                yield head.strip()
        if len(current) + len(sentence) > limit and current.strip():
            yield current.strip()
            current = ''
        current += sentence
    if current.strip():
        yield current.strip()


def reference(voice):
    # Named references only: no caller-chosen filesystem paths or remote URLs.
    if not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.wav)?', voice):
        raise HTTPException(422, 'voice must name a WAV file under /cache/voices')
    path = (VOICE_ROOT / (voice if voice.endswith('.wav') else voice + '.wav')).resolve()
    if path.parent != VOICE_ROOT.resolve() or not path.is_file():
        raise HTTPException(422, 'reference WAV does not exist')
    info = sf.info(str(path))
    if info.format != 'WAV' or not 1 <= info.duration <= 30 or info.samplerate < 16000:
        raise HTTPException(422, 'reference must be a 1–30 second WAV at >=16 kHz')
    return path


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=12000)
    model: Literal['cosyvoice3'] = 'cosyvoice3'
    voice: str = Field(min_length=1)
    prompt_text: str = Field(min_length=1, max_length=2000)
    language: str = 'auto'
    response_format: Literal['wav', 'pcm'] = 'wav'
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    stream: Literal[False] = False


def load_model():
    """Called only while the request lock is held; disk caches remain intact."""
    global model, loaded_sample_rate
    from cosyvoice.cli.cosyvoice import AutoModel
    model = AutoModel(model_dir=os.environ['COSYVOICE_MODEL_DIR'], fp16=False,
                      load_trt=False, load_vllm=False)
    if next(model.model.llm.parameters()).device.type != 'cuda':
        raise RuntimeError('CosyVoice LLM did not load onto the GPU')
    torch.cuda.synchronize()
    loaded_sample_rate = model.sample_rate


def unload_model():
    global model, loaded_sample_rate
    initialized = torch.cuda.is_initialized()
    try:
        if initialized:
            torch.cuda.synchronize()
    finally:
        model = None
        loaded_sample_rate = None
        gc.collect()
        if initialized:
            torch.cuda.empty_cache()


@asynccontextmanager
async def lifespan(app):
    global server_ready
    if not torch.version.hip:
        raise RuntimeError('CosyVoice requires the ROCm GPU; CPU fallback is disabled')
    server_ready = True
    try:
        yield
    finally:
        server_ready = False
        with lock:
            unload_model()


app = FastAPI(lifespan=lifespan)


@app.get('/health')
def health():
    if not server_ready:
        raise HTTPException(503, 'server not ready')
    return {'ready': True, 'model': 'cosyvoice3', 'device': 'cuda',
            'runtime': 'rocm', 'sample_rate': loaded_sample_rate,
            'loaded': model is not None, 'busy': lock.locked()}


def synthesize(request, wav):
    """Return CPU-encoded bytes; this frame must unwind before GPU cleanup."""
    if model is None:
        load_model()
    prompt = 'You are a helpful assistant.<|endofprompt|>' + request.prompt_text
    audio = []
    with torch.inference_mode():
        for text in chunks(request.input):
            outputs = model.inference_zero_shot(text, prompt, str(wav),
                    stream=False, speed=request.speed, text_frontend=False)
            try:
                for output in outputs:
                    audio.append(output['tts_speech'].detach().float().cpu().numpy().reshape(-1))
            finally:
                # A suspended inference generator can retain GPU tensors.
                close = getattr(outputs, 'close', None)
                if close is not None:
                    close()
    if not audio:
        raise RuntimeError('model produced no audio')
    samples = np.concatenate(audio)
    if not np.isfinite(samples).all() or samples.size < model.sample_rate // 10 or np.max(np.abs(samples)) < 1e-5:
        raise RuntimeError('model produced invalid or silent audio')
    result = io.BytesIO()
    sf.write(result, samples, model.sample_rate, format='WAV' if request.response_format == 'wav' else 'RAW', subtype='PCM_16', endian='LITTLE')
    return Response(result.getvalue(), media_type='audio/wav' if request.response_format == 'wav' else 'application/octet-stream',
                    headers={'X-Audio-Sample-Rate': str(model.sample_rate)})


@app.post('/v1/audio/speech')
def speech(request: SpeechRequest):
    if not server_ready:
        raise HTTPException(503, 'server not ready')
    if request.language not in LANGUAGES:
        raise HTTPException(422, 'unsupported language')
    if not request.input.strip() or not request.prompt_text.strip():
        raise HTTPException(422, 'input and prompt_text must contain text')
    if '<|' in request.input or '<|' in request.prompt_text:
        raise HTTPException(422, 'supply plain text without model control tokens')
    wav = reference(request.voice)
    if not lock.acquire(blocking=False):
        raise HTTPException(429, 'GPU busy; retry after current request', headers={'Retry-After': '10'})
    error = None
    try:
        try:
            response = synthesize(request, wav)
        except Exception as exc:
            logging.exception('CosyVoice synthesis failed')
            # Unwound load/inference frames may retain model/tensor references.
            # Clear them and leave the exception handler before collecting GPU memory.
            traceback.clear_frames(exc.__traceback__)
            error = HTTPException(500, 'synthesis failed; inspect server logs')
    finally:
        try:
            unload_model()
        finally:
            lock.release()
    if error is not None:
        raise error from None
    return response


from tts_runtime import RecycleAfterRequest
app = RecycleAfterRequest(app, paths={'/v1/audio/speech'})


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8007, workers=1)
