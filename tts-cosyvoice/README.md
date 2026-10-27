# CosyVoice3 private speech API

ROCm 7.2 deployment of [official CosyVoice](https://github.com/QwenAudio/CosyVoice)
revision `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc` and
[Fun-CosyVoice3-0.5B-2512](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512)
revision `29e01c4e8d000f4bcd70751be16fa94bf3d85a18` (Apache 2.0).
Model inference uses the ROCm base's existing PyTorch wheels in FP32; ONNX
speaker/token extraction uses CPU. TensorRT, vLLM and CUDA ONNX are disabled.
The base is pinned by digest; upstream Matcha-TTS is pinned by its gitlink.

Set `BIND_ADDRESS` and `CACHE_DIR` in `.env`. Bind only to a private host
address. There is no public ingress or authentication; callers must be trusted
LAN clients. Placement is controlled by deployment configuration.

Reference WAVs and models remain outside Git. Put `default-en.wav` and
`default-zh.wav` in `${CACHE_DIR}/voices/`, with matching transcript `.txt`
sidecars for operator records. References must be 1–30 seconds, >=16 kHz WAV.
Use only voices you are authorized to use. The API requires the exact spoken
transcript as `prompt_text`; sidecars are not read automatically.

```sh
curl --fail-with-body http://127.0.0.1:18007/health
curl --fail-with-body http://127.0.0.1:18007/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"cosyvoice3","input":"The story begins here.","language":"en","voice":"default-en","prompt_text":"The exact reference transcript.","response_format":"wav","speed":1.0}' \
  -o /tmp/cosyvoice.wav
```

`voice` is a filename stem under `/cache/voices`; paths and URLs are rejected.
`input` is bounded to 12,000 characters. Complete sentences are grouped up to
240 characters; oversized sentences split at spaces/commas where possible.
Chunks synthesize consecutively without artificial inter-chunk padding. The
supplied text determines pronunciation language; `language` validates the caller's
selection (`auto`, zh/en/ja/ko/de/es/fr/it/ru) and does not translate text.
The adapter bypasses text normalization to preserve the script; spell out numbers
and abbreviations in narration copy. `speed` is 0.5–2.0. Streaming is unsupported.
Output is mono WAV PCM16 or raw PCM16 little endian at the model sample rate,
returned in `X-Audio-Sample-Rate`.

One request owns the GPU at a time; simultaneous requests receive HTTP 429 with
`Retry-After: 1`. The model loads lazily inside this lock. After each request,
successful or failed, the adapter synchronizes GPU work, releases model
references, runs garbage collection and empties PyTorch's GPU cache before releasing
the lock. Successful audio is fully encoded into CPU response bytes first.
Every API request pays the model reload cost, including each separate request
for a book chunk; sentence chunks within one API request share one model load.
The Hugging Face/model files and voice references remain cached on disk.
ROCm/PyTorch context memory can remain allocated after unloading.

This lock covers only CosyVoice. Coordinate other GPU work externally.
`/health` checks server readiness without loading weights; it reports `loaded`
and `busy`, with `sample_rate: null` while unloaded. Startup checks that
PyTorch is a ROCm build without initializing the GPU.
Actual GPU availability is verified when a speech request loads the model. A healthy
unloaded server does not prove that the next model load or synthesis will pass.
First launch downloads the pinned snapshot; further launches reuse the Hugging
Face cache and verify files through its snapshot API. Offline mode is enabled
after resolving the snapshot. Keep the 24 GiB memory/swap caps.

Build and validate:

```sh
cp .env.example .env  # Set the cache path and private bind address.
docker build --network host -t local/tts-cosyvoice:074ca6dc-rocm7.2-recycle1 .
BIND_ADDRESS=127.0.0.1 CACHE_DIR=/srv/tts-cosyvoice \
  docker compose config --quiet
docker run --rm --entrypoint python \
  -v "$PWD:/tests:ro" local/tts-cosyvoice:074ca6dc-rocm7.2-recycle1 \
  /tests/test_adapter.py
```

Start with `docker compose up -d` after validating configuration. Roll back by stopping
only `tts-cosyvoice` and reverting its image tag. Preserve the cache
and voice references; never use `down -v`.

For a slow initial model transfer, `download_model.py` offers eight bounded
16 MiB HTTP range connections. Stop this stack and any separate prefetch helper
first so only one downloader owns these cache files. Existing Hugging Face
partials and range parts are retained; each new complete weight must match the
official pinned LFS SHA256 before it becomes a cache blob. Run with the built
image and no GPU devices:

```sh
docker run --rm --network host --entrypoint python \
  -e HF_HOME=/cache/huggingface -e HF_HUB_DISABLE_XET=1 \
  -v /srv/tts-cosyvoice:/cache \
  -v "$PWD:/tools:ro" local/tts-cosyvoice:074ca6dc-rocm7.2-recycle1 \
  /tools/download_model.py --cache /cache/huggingface --workers 8
```

This changes transfer concurrency, not the model revision. Coordinate large
model transfers on the same uplink; parallel connections cannot increase a
saturated network's capacity. Startup checks all six pinned weight file sizes.

`--transport https://hf-mirror.com` is an optional mirror for weight-file bytes
when the official transport is slow. Metadata and checksum authority remain the
pinned official Hugging Face API; full assembled SHA256 must still match.
The default transport remains `https://huggingface.co`.

The pinned frontend's optional normalizer initialization is disabled by a checked
patch: it otherwise fetches unpinned ModelScope resources during startup.
Prepared target and reference text both use `text_frontend=False`, which returns
before normalization. Real English and Chinese zero-shot requests pass with this
configuration. On the tested AMD system, the first tested 5.8 s English and 11.12 s Chinese WAVs
took 23.90 s and 40.41 s respectively, including first-run kernel work; this is
working FP32 ROCm inference, not a claim of real-time generation.

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
transport failure. The supervisor bounds worker shutdown to 30 seconds; Compose
allows 45 seconds for a container stop. The recycle deadline starts after
request cleanup signals completion; it is not an inference timeout.

Run `python -m unittest -v test_runtime.py` to test listener preservation,
stream completion, draining, bounded shutdown, and restart backoff. The identical
`tts_runtime.py` and its test are copied into each standalone TTS build context.
