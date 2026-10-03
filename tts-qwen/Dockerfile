FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 sox && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.8.22
RUN --mount=type=cache,target=/root/.cache/uv UV_HTTP_TIMEOUT=600 UV_CONCURRENT_DOWNLOADS=4 uv pip install --system torch==2.9.1 torchaudio==2.9.1 --index-url https://pypi.org/simple
RUN --mount=type=cache,target=/root/.cache/uv printf 'torch==2.9.1\ntorchaudio==2.9.1\n' > /tmp/constraints.txt && UV_HTTP_TIMEOUT=600 UV_CONCURRENT_DOWNLOADS=4 uv pip install --system -c /tmp/constraints.txt qwen-tts==0.1.1 fastapi==0.115.14 uvicorn==0.34.3 soundfile==0.13.1 numpy==2.2.6 prometheus-client==0.22.1 python-multipart==0.0.20
WORKDIR /app
# Upstream server and preset voices, pinned by source archive digest.
ADD --checksum=sha256:5dfe0f6c31c090afd21fbdda5f2d7276a49cce6487b5a80a08c445ec384e95e7 https://codeload.github.com/malaiwah/qwen3-tts-server/tar.gz/2cec3c5f9b5a2927633388e09404d7d000487f53 /tmp/server.tar.gz
RUN tar -xzf /tmp/server.tar.gz --strip-components=1 -C /app \
    qwen3-tts-server-2cec3c5f9b5a2927633388e09404d7d000487f53/server.py \
    qwen3-tts-server-2cec3c5f9b5a2927633388e09404d7d000487f53/voice_registry.py \
    qwen3-tts-server-2cec3c5f9b5a2927633388e09404d7d000487f53/LICENSE \
    qwen3-tts-server-2cec3c5f9b5a2927633388e09404d7d000487f53/assets && rm /tmp/server.tar.gz
# Adds on-demand GPU sharing (lazy start, idle-unload, free-VRAM gate) so this can
# coexist with Strata on the same card. Off by default; see patch_server.py and
# lazy loading, idle unloading, and a VRAM gate. Fails the build if upstream server.py
# drifts from what the patch expects, instead of silently no-op'ing.
COPY patch_server.py /tmp/patch_server.py
RUN python /tmp/patch_server.py
ENTRYPOINT ["python", "/app/server.py"]
CMD ["--host", "0.0.0.0", "--port", "8001"]
