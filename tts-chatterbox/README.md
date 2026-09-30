# Chatterbox Multilingual on ROCm

This stack adapts the upstream
[Chatterbox-TTS-Server](https://github.com/devnen/Chatterbox-TTS-Server)
Strix Halo recipe to AMD's PyTorch ROCm base. Server and engine Git revisions
and the multilingual model snapshot are pinned in `Dockerfile`. Model configuration selects Multilingual, not Turbo.
The upstream dtype corrections are checked and applied during the build.
Chinese and Japanese tokenizer helpers are installed explicitly, with librosa
0.11.0 as required by the engine. Chinese segmentation weights persist under
`CACHE_DIR/pkuseg` after their first download.

Copy `.env.example` to `.env` and set a **private** bind address and cache path.
Run `docker compose build` and `docker compose up -d`. The GPU requires `/dev/kfd`
and `/dev/dri`. Verify `torch.cuda.is_available()` and a real speech request;
PyTorch uses the `cuda` device name for ROCm too. Avoid unnecessary architecture
overrides. Initial weights download into the persistent cache.

`POST /v1/audio/speech` on port 18004 accepts model, input, voice (e.g.
`Emily.wav`), language, and `response_format: wav`. Reference voices bundled by
the upstream server retain their upstream provenance. Do not expose this port
through a public proxy. Only connect trusted private clients.
