# Chatterbox Multilingual on ROCm

This stack adapts the upstream
[Chatterbox-TTS-Server](https://github.com/devnen/Chatterbox-TTS-Server)
for AMD's PyTorch ROCm base. Server and engine Git revisions
and the multilingual model snapshot are pinned in `Dockerfile`. Model configuration selects Multilingual, not Turbo.
The upstream dtype corrections are checked and applied during the build.
Chinese and Japanese tokenizer helpers are installed explicitly, with librosa
0.11.0 as required by the engine. Chinese segmentation weights persist under
`CACHE_DIR/pkuseg` after their first download.

The pinned engine registers three attention hooks for each multilingual request.
`patch_engine.py` removes those hooks and retained tensors after inference,
including failures, to prevent memory growth over long books. Its source checks
fail the image build if an upstream update changes the patch targets. The memory
and swap ceilings are equal: a genuine memory overrun fails instead of spending
hours reclaiming swapped tensors while the speech API is unresponsive.
Run `docker run --rm --network none --entrypoint python -v "$PWD/test_hooks.py:/tmp/test_hooks.py:ro" local/tts-chatterbox:915ae289-rocm7.2-recycle1 /tmp/test_hooks.py`
to check 100 request lifecycles, failure cleanup, and preservation of unrelated
hooks without downloading a model or using a GPU.

`patch_lifecycle.py` checks the pinned server's startup and API routes before
installing `lifecycle.py`. Startup leaves the model unloaded. Each `POST /tts`
(custom API, including streaming) or `POST /v1/audio/speech` loads it from the
persistent cache, generates speech, then clears conditioning tensors and unloads
the model after the complete ASGI response and background work finish. Cleanup
also runs on failed loads, inference failures, stream errors and disconnects.
The upstream unload releases model references, collects garbage and empties the
GPU allocator cache. Model reload latency is paid on every speech request; the
first request may additionally download weights.

One request owns the model at a time. All overlapping POST/PUT/PATCH/DELETE
requests receive HTTP 429 with `Retry-After: 1`; retry after the current request
completes. This includes settings, voice uploads, `/api/unload` and
`/restart_server`, so they cannot race with speech. Manual reload still validates
the configured model but returns to unloaded state after its response. A failed
lazy load returns HTTP 503 and a later request retries loading. Keep the pinned
single Uvicorn worker; ownership is process-local.

Idle `/api/model-info` truthfully reports `loaded: false`, `device: null` and
`sample_rate: null`. Its `type`, language list and capability flags describe the
configured model, allowing the UI to select multilingual languages before the
first request. `metadata_source: configured` distinguishes this from loaded
metadata (`metadata_source: loaded`). The checked engine patch reads the current
model selector and uses the engine's supported-language table without loading
weights. It snapshots metadata during load/unload and replaces upstream's
transient deletion of the model global with direct assignment, keeping health
queries safe while GPU tensor destructors run. The Compose
healthcheck accepts this idle state; when loaded it requires Multilingual on
`cuda`. Health alone does not prove that a GPU model can load: verify a real
speech request. Read-only discovery and health calls never load the model.

Run the CPU-only lifecycle suite without network or GPU access:

```sh
python -m unittest -v test_lifecycle.py
docker run --rm --network none --entrypoint python \
  -v "$PWD/test_lifecycle.py:/app/test_lifecycle.py:ro" \
  -v "$PWD/patch_lifecycle.py:/app/patch_lifecycle.py:ro" \
  local/tts-chatterbox:915ae289-rocm7.2-recycle1 -m unittest -v test_lifecycle.py
```

Copy `.env.example` to `.env` and set a **private** bind address and cache path.
Run `docker compose build` and `docker compose up -d`. The GPU requires `/dev/kfd`
and `/dev/dri`. Verify `torch.cuda.is_available()` and a real speech request;
PyTorch uses the `cuda` device name for ROCm too. Avoid unnecessary architecture
overrides. Initial weights download into the persistent cache.

`POST /v1/audio/speech` on port 18004 accepts model, input, voice (e.g.
`Emily.wav`), language, and `response_format: wav`. Reference voices bundled by
the upstream server retain their upstream provenance. Do not expose this port
through a public proxy. Only connect trusted private clients.

## GPU context lifetime

A CPU-only supervisor keeps the listening socket open. Each speech request is
served by one Uvicorn worker; after the full response and background cleanup,
that worker exits and is replaced. Process exit releases ROCm contexts and
queues that survive model deletion and allocator-cache cleanup. The supervisor
and fresh idle workers never run GPU operations. Model and voice files remain
on disk. Worker startup adds latency to every separate request.

Responses that trigger recycling use `Connection: close`. Overlapping writes
receive HTTP 429 with `Retry-After: 1`; clients should retry those explicit
responses. Connections queued on the listening socket wait for the replacement
worker. Do not automatically replay a synthesis request after an ambiguous
transport failure. After the completion notification, the supervisor bounds worker shutdown to
30 seconds; this is not an inference timeout. Compose
allows 45 seconds for a container stop. An interrupted streaming request waits
for its active synthesis thread before model cleanup and worker recycling.

Run `python -m unittest -v test_runtime.py` to test listener preservation,
stream completion, draining, bounded shutdown, and restart backoff. The identical
`tts_runtime.py` and its test are copied into each standalone TTS build context.
