# Fish S2 Pro private evaluation

**PRIVATE NONCOMMERCIAL EVALUATION ONLY.** Deployment and successful auditions
provide no commercial or publication clearance. Review the pinned
[Fish Audio Research License](https://huggingface.co/fishaudio/s2-pro/blob/1de9996b6be38b745688de084d87a5633f714e4e/LICENSE.md)
before any other use. Keep weights, references, transcripts and generated audio
outside Git.

Official S2-only source: `bdd39477922a89b904c6f2c7397f2a9bbcc60a0c`.
Official `fishaudio/s2-pro` snapshot: `1de9996b6be38b745688de084d87a5633f714e4e`.
Built image: `local/tts-fish:bdd39477-rocm7.2-eager2`, verified ID
`sha256:a282dabd57fcf408af864d7b776f18179b2b65718d8737fe4e11d8a30e266db5`.
The Dockerfile pins the ROCm 7.2 / PyTorch 2.9.1 base digest and verifies the
source archive hash. Runtime requires ROCm; torch.compile is disabled and
attention uses math SDPA. Drama 3 is not used.

Set `BIND_ADDRESS` to a private host address, `CACHE_DIR` to persistent cache,
and `VOICES_DIR` to read-only synthetic references, for example
`/srv/tts-fish/cache` and `/srv/tts-fish/voices`. The API uses port 18008.
No public ingress is configured.
The singleton stack starts on demand, never restarts automatically, and has
32 GiB memory and total memory-plus-swap limits. Coordinate other GPU jobs.

```bash
docker build --network host -t local/tts-fish:bdd39477-rocm7.2-eager2 .
docker compose config --quiet
docker compose up -d --no-build
```

Reference pairs are `ryan-en.wav`/`.txt` and `uncle-fu-zh.wav`/`.txt`.
They must contain finite audio lasting 1–30 seconds at 8–192 kHz and a nonempty
transcript. Malformed references return HTTP 422 before GPU inference.

`GET /health` reports the loaded revision and actual GPU/HIP backend.
`POST /v1/audio/speech` accepts `input`, `model: "s2-pro"`,
`voice: "default-en"` or `"default-zh"`, optional `language: "en"` or `"zh"`,
`response_format: "wav"`, `speed: 1.0`, and optional `seed`.
Voice aliases `ryan-en` and `uncle-fu-zh` are also accepted. Pronunciation follows
input text. Responses are complete mono PCM16 WAVs at 44.1 kHz; only WAV and
speed 1 are supported. One request runs at a time; simultaneous requests receive
HTTP 429.

Input is limited to 4,000 Unicode characters. The adapter splits it into
sentence-aware chunks of at most 200 Unicode characters without changing text
or order. It encodes the reference once per request, generates each text chunk
with that same reference, immediately decodes each emitted sample, and joins
CPU audio afterward. This bounds each decoder invocation; upstream's
`chunk_length` alone does not split an oversized plain-text turn.
Semantic inference uses BF16, codec parameters and reference encoding use FP32,
and decoding uses BF16 autocast, matching upstream's decoder precision.

Compose enables `MIOPEN_FIND_MODE=FAST` and persists MIOpen databases and kernels
at `/cache/miopen/db` and `/cache/miopen/kernels`, creating both on startup.
A controlled eager1 cold-cache English request fell from 190.78 to 26.79 seconds
with FAST; semantic sampling remained about 21.8 seconds. Final eager2 short
English/Chinese requests took 24.73/23.18 seconds and produced valid, unclipped
6.92/6.50-second WAVs. Keep deployment-specific validation records with operational notes.

Optional prefetch:
`download_model.py --cache /cache --workers 8` uses bounded HTTP ranges and
checks official full-file LFS SHA256 values before atomic publication.
`--transport https://hf-mirror.com` changes weight transport only; metadata and
hash authority remain official Hugging Face. Stop any other Fish prefetch first;
do not run simultaneous cache writers. Complete snapshots restart using
local-only access after required files, exact weight sizes and shard index are
checked. Incomplete snapshots fetch the pinned revision with two workers.
The tokenizer uses JSON artifacts through AutoTokenizer; no legacy `.tiktoken`
file is required.

Run CPU contract tests against the built image's adapter:

```bash
docker run --rm -v "$PWD/test_adapter.py":/test_adapter.py:ro -e PYTHONPATH=/app \
  local/tts-fish:bdd39477-rocm7.2-eager2 python /test_adapter.py
```

Nine tests cover request/reference validation, busy rejection and lock cleanup,
cache selection, and English/Chinese/long-input chunk boundaries and exact text
reconstruction. `codec_smoke.py` independently probes the pinned codec on ROCm;
codec reconstruction alone does not establish semantic narration quality.
Stop/remove only this service for rollback and retain cache/references.
