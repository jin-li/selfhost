# Qwen3-TTS 1.7B

Use a digest-pinned image from
[malaiwah/qwen3-tts-server](https://github.com/malaiwah/qwen3-tts-server).
This serves `Qwen/Qwen3-TTS-12Hz-1.7B-Base` with preset-voice embeddings and an
OpenAI-compatible speech endpoint. Copy `.env.example` to `.env` and set the
image digest, persistent data directory, and private bind address.

With NVIDIA Container Toolkit installed:

```sh
docker compose -f docker-compose.yml -f compose.cuda.yaml up -d
```

Verify GPU availability and `/health` model readiness, then generate a real
`POST /v1/audio/speech` request. Port 18005 must remain private. Models persist
in the cache; registered voices persist separately. Ollama is not required.

`bootstrap.py` pins the model snapshot and reuses a complete local cache without
network access. An incomplete cache triggers a download before the unchanged
upstream server starts. After prefetching the snapshot, `HF_HUB_OFFLINE=1` can
enforce offline operation. Update the revision, expected weight sizes, and
converter profile revision together when upgrading the model.

A reproducible source build is also available. It pins the unchanged server
archive, PyTorch 2.9.1 (CUDA 12.8), and Qwen TTS package 0.1.1. It uses the
upstream server's supported PyTorch SDPA backend, without Flash Attention.
Set `QWEN_IMAGE=local/tts-qwen:2cec3c5f-cu128-sdpa` in `.env`, then run:

```sh
docker compose -f docker-compose.yml -f compose.build.yaml build tts-qwen
docker compose -f docker-compose.yml -f compose.cuda.yaml up -d
```

The build uses host networking to download dependencies; runtime networking is
unchanged. Verify real CUDA synthesis before using a newly built image.
