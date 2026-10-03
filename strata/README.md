# Strata (Qwen3.8-Flash-Next)

Upstream: [Niko1221/Strata](https://github.com/Niko1221/Strata). Runs the
125B-parameter Qwen3.8-Flash-Next MoE model locally via a custom llama.cpp/ggml
-derived engine (not a plain GGUF server — see `docs/HOW_IT_WORKS.md`
upstream). Needs an NVIDIA RTX 20/30/40/50-series GPU with 12+ GB VRAM.

A reproducible source build is the only option here (upstream publishes no
registry image). It pins an exact upstream commit by digest-verified tarball,
configured by `compose.build.yaml` for Blackwell/RTX 50 (`sm_120`) to keep
the build short. Adjust `CUDA_ARCHITECTURES` for a different supported GPU:

```sh
docker compose -f docker-compose.yml -f compose.build.yaml build strata
```

With NVIDIA Container Toolkit installed:

```sh
docker compose -f docker-compose.yml -f compose.cuda.yaml up -d
```

Without the toolkit, supply your own Compose override with the matching GPU
devices and driver-library mounts instead of `compose.cuda.yaml`. Those mounts
are host-specific and are not provided by the portable base.

Copy `.env.example` to `.env` and set the image tag, persistent data
directory, private bind address, model, and context length first. Port 18006
must remain private — never add a Traefik/Cloudflared route for it.

The first start downloads the chosen quant (~60-85 GB) into `$DATA_DIR` before
the server answers `/health`; subsequent starts reuse it. `LOW_RAM` is left on
`auto` deliberately: the container has no memory cgroup limit here, so
`/proc/meminfo` correctly reports the host's real RAM. If a memory cap is ever
added to the compose file, `LOW_RAM` must be set explicitly (`on`), since the
auto-detection would otherwise read the host's uncapped total instead of the
container's limit.

Serves one request at a time. Switching `STRATA_MODEL` to a quant not yet
installed re-runs setup on next start; set `REINSTALL=1` to change context/
vision/KV for an already-installed quant.
