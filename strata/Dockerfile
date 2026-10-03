# syntax=docker/dockerfile:1
#
# Strata: Qwen3.8-Flash-Next (125B MoE) on NVIDIA GPUs, 12+ GB VRAM. Upstream:
# https://github.com/Niko1221/Strata. The engine compiles during docker build,
# so the first container start only downloads the model (~70 GB).
#
# Unlike upstream's own Dockerfile (which COPYs a live checkout), this pins an
# exact source commit by digest-verified tarball, matching this repo's house
# style for third-party builds (see selfhost/tts-qwen/Dockerfile).
#
# Build (narrowed to the example target GPU architecture, Blackwell/RTX 50 = sm_120):
#   docker build --network host -t local/strata:d9ab843-cuda13 \
#     --build-arg CUDA_ARCHITECTURES=120 --build-arg BUILD_VISION=0 .
#
# Run env vars (read by docker-entrypoint.sh): FAMILY, MODEL, CONTEXT, VISION,
# KV, GPU/GPUS, LAYER_SPLIT, LOW_RAM, HOST, PORT, API_KEY. See compose files.

FROM nvidia/cuda:13.0.0-devel-ubuntu24.04@sha256:435220c0fef35cbf712e11999f8670a83835ef3cdd18564e5e8122f83078c88c

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 LANG=C.UTF-8 STRATA_EXECV=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git libatomic1 libgomp1 \
        python3 python3-pip python3-venv unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/strata

# Strata source at commit d9ab8435f654c368c586340d490915f6addf56a3, pinned by
# digest-verified tarball instead of `git clone` (reproducible, no live HEAD).
ADD --checksum=sha256:d51e94e73bd1859fc8686d4b06750a2bc547ab790f6bff38adfee407f3566a34 \
    https://codeload.github.com/Niko1221/Strata/tar.gz/d9ab8435f654c368c586340d490915f6addf56a3 \
    /tmp/strata.tar.gz
RUN tar -xzf /tmp/strata.tar.gz --strip-components=1 -C /opt/strata && rm /tmp/strata.tar.gz

ARG CUDA_ARCHITECTURES=120
ARG BUILD_VISION=0

RUN python3 -m venv .venv \
    && .venv/bin/pip install --no-cache-dir --upgrade pip \
    && .venv/bin/pip install --no-cache-dir -r requirements.txt \
    && chmod +x setup.sh docker-entrypoint.sh

# llama.cpp at the pinned commit, then the engine (and optionally the vision
# encoder), built exactly the way setup.py builds them upstream.
RUN .venv/bin/python - <<'PYEOF'
import json, os, pathlib, shutil
import setup

llama = setup.get_llama_cpp()
nvcc, _ = setup.find_nvcc()
arch = os.environ.get("CUDA_ARCHITECTURES", "120").strip().strip('"').replace(",", ";")
vision = "gpu" if os.environ.get("BUILD_VISION", "0") == "1" else "none"

setup.cmake_build(setup.ROOT, setup.ROOT / "build", "strata",
    ["-DSTRATA_ENABLE_CUDA=ON", "-DSTRATA_BUILD_TESTS=OFF",
     f"-DCMAKE_CUDA_ARCHITECTURES={arch}", f"-DCMAKE_CUDA_COMPILER={nvcc}",
     f"-DSTRATA_GGML_DIR={llama}"], None, "build-strata.bat")
if vision != "none":
    setup.cmake_build(setup.ROOT / "tools" / "vision", setup.ROOT / "build-vision", "strata-vision",
        [f"-DLLAMA_DIR={llama}", "-DSTRATA_VISION_CUDA=ON",
         f"-DCMAKE_CUDA_ARCHITECTURES={arch}", f"-DCMAKE_CUDA_COMPILER={nvcc}"], None, "build-vision.bat")

eng = setup.ROOT / "engine"
eng.mkdir(exist_ok=True)
shutil.copy2(setup.ROOT / "build" / setup.EXE, eng / setup.EXE)
if vision != "none":
    shutil.copy2(setup.ROOT / "build-vision" / "bin" / setup.VEXE, eng / setup.VEXE)
bindir = pathlib.Path(nvcc).parent
meta = {"source": "local", "version": setup.source_version(),
        "archs": [int(a.split("-")[0]) for a in arch.split(";") if a.split("-")[0].isdigit()], "vision": vision,
        "cuda_dirs": [str(d) for d in (bindir, bindir / "x64", bindir.parent / "lib64") if d.is_dir()],
        "src": setup.source_hash(setup.ENGINE_SOURCES),
        "vision_src": setup.source_hash(setup.VISION_SOURCES) if vision != "none" else None}
(eng / "BUILD.json").write_text(json.dumps(meta, indent=1))
PYEOF

RUN rm -rf build build-vision

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=3 \
  CMD curl -fs "http://127.0.0.1:${PORT:-8080}/health" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
