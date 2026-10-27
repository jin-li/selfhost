FROM rocm/pytorch:rocm7.2_ubuntu24.04_py3.12_pytorch_release_2.9.1@sha256:a3b65813621095e3389269417e963725b59310184588c9d2490d44e6e83fa01c
ARG FISH_REV=bdd39477922a89b904c6f2c7397f2a9bbcc60a0c
ENV PYTHONUNBUFFERED=1 HF_HOME=/cache HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 git && rm -rf /var/lib/apt/lists/*
ADD https://codeload.github.com/fishaudio/fish-speech/tar.gz/bdd39477922a89b904c6f2c7397f2a9bbcc60a0c /tmp/fish.tar.gz
RUN echo "1d873180bdac22474cf326676a17c15af113124bf5fef960fc06a0f654945767  /tmp/fish.tar.gz" | sha256sum -c - && mkdir /app && tar -xzf /tmp/fish.tar.gz --strip-components=1 -C /app && rm /tmp/fish.tar.gz
WORKDIR /app
# Keep AMD's installed GPU wheels. Install runtime dependencies only.
RUN python -c 'import importlib.metadata as m; print("\n".join(n+"=="+m.version(n) for n in ("torch","torchaudio","torchvision","triton")))' > /tmp/torch-constraints.txt && \
    python -m pip install -c /tmp/torch-constraints.txt transformers==4.57.3 hydra-core==1.3.2 natsort==8.4.0 einops==0.8.2 librosa==0.11.0 loguru==0.7.3 pyrootutils==1.0.4 resampy==0.4.3 'einx[torch]==0.2.2' pydub==0.25.1 opencc-python-reimplemented==0.1.7 tiktoken==0.12.0 pydantic==2.9.2 cachetools==6.2.6 descript-audio-codec==1.0.0 safetensors==0.7.0 soundfile==0.13.1 fastapi==0.115.6 uvicorn==0.34.0 && \
    python -m pip install 'protobuf==5.29.6'
RUN python -m pip install --no-deps loralib==0.1.2
RUN python -m pip install -c /tmp/torch-constraints.txt lightning==2.5.5
# gfx1151 needs the portable attention path. No torch.compile or fused CUDA ops.
RUN python - <<'PY'
from pathlib import Path
p = Path('fish_speech/models/text2semantic/llama.py')
s = p.read_text()
assert s.count('with sdpa_kernel(SDPBackend.FLASH_ATTENTION):') == 1
p.write_text(s.replace('with sdpa_kernel(SDPBackend.FLASH_ATTENTION):', 'with sdpa_kernel(SDPBackend.MATH):'))
PY
RUN python -c 'from fish_speech.models.text2semantic.inference import init_model, generate_long; from fish_speech.models.dac.inference import load_model'
COPY server.py /app/server.py
EXPOSE 8008
CMD ["python", "server.py"]
