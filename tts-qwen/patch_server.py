"""Add on-demand GPU sharing to the pinned qwen3-tts-server: lazy start, idle-unload,
and a free-VRAM gate so it coexists with other GPU programs (e.g. Strata) instead of
fighting them for memory. Mirrors the equivalent feature already built into Strata's
own serve/server.py (idle_unload_s / min_free_vram_mib / lazy). Off by default
(QWEN3_TTS_LAZY_LOAD=0, QWEN3_TTS_IDLE_UNLOAD_S=0) — unmodified server.py behavior
unless explicitly enabled via env vars.
"""
from pathlib import Path
import py_compile


def replace_once(path: Path, old: str, new: str) -> None:
    replace_n(path, old, new, 1)


def replace_n(path: Path, old: str, new: str, n: int) -> None:
    source = path.read_text()
    count = source.count(old)
    if count != n:
        raise RuntimeError(f"Pinned server patch no longer matches {path.name} ({count} occurrences, expected {n})")
    path.write_text(source.replace(old, new, n))


_HELPERS = '''
# -----------------------------------------------------------------------
# On-demand loading: lazy start, idle-unload, VRAM-aware reload refusal.
# Adds local GPU sharing through lazy loading, idle unloading, and a VRAM gate.
# Off unless QWEN3_TTS_LAZY_LOAD / QWEN3_TTS_IDLE_UNLOAD_S are set.
# -----------------------------------------------------------------------

_ON_DEMAND_LAZY = os.getenv("QWEN3_TTS_LAZY_LOAD", "0") == "1"
_ON_DEMAND_IDLE_UNLOAD_S = float(os.getenv("QWEN3_TTS_IDLE_UNLOAD_S", "0") or 0)
_ON_DEMAND_MIN_FREE_VRAM_MIB = int(os.getenv("QWEN3_TTS_MIN_FREE_VRAM_MIB", "0") or 0)

_LOAD_LOCK: asyncio.Lock
_last_request_at: float = 0.0


def _free_vram_mib() -> Optional[float]:
    if _DEVICE != "cuda" or not torch.cuda.is_available():
        return None
    free_b, _total_b = torch.cuda.mem_get_info()
    return free_b / (1024 * 1024)


def _unload_model_sync() -> None:
    """Release GPU/RAM between requests; the voice registry keeps its preset/custom
    metadata — only the model binding is dropped."""
    global _model, _model_ready
    _model = None
    _model_ready = False
    _voice_registry.bind_model(None)
    if _DEVICE == "cuda":
        torch.cuda.empty_cache()
    if _METRICS_AVAILABLE:
        _M_MODEL_READY.set(0)
    logger.info("Model unloaded (idle %.0fs); the next request loads it again.", _ON_DEMAND_IDLE_UNLOAD_S)


async def _ensure_loaded() -> None:
    """Called at the top of every synthesis endpoint: records activity for the idle
    timer and lazily (re)loads the model, refusing instead of OOM-fighting another
    program for VRAM."""
    global _last_request_at
    _last_request_at = time.time()
    if _model_ready:
        return
    async with _LOAD_LOCK:
        if _model_ready:                      # loaded by another request while we waited
            return
        free_mib = _free_vram_mib()
        if free_mib is not None and _ON_DEMAND_MIN_FREE_VRAM_MIB and free_mib < _ON_DEMAND_MIN_FREE_VRAM_MIB:
            raise HTTPException(
                503,
                f"the GPU is in use by another program: {free_mib:.0f} MiB of VRAM free, "
                f"the model needs {_ON_DEMAND_MIN_FREE_VRAM_MIB} (QWEN3_TTS_MIN_FREE_VRAM_MIB) "
                "- retry once that is free",
            )
        logger.info("Loading on demand (was unloaded) ...")
        await asyncio.to_thread(_load_model_sync, _DEVICE, _DEVICE == "cuda")


async def _idle_unload_loop() -> None:
    interval = max(1.0, min(30.0, _ON_DEMAND_IDLE_UNLOAD_S / 4))
    while True:
        await asyncio.sleep(interval)
        if not _model_ready or _INFER_SEM.locked():
            continue
        if time.time() - _last_request_at < _ON_DEMAND_IDLE_UNLOAD_S:
            continue
        async with _LOAD_LOCK:
            if _model_ready and not _INFER_SEM.locked() and \\
               time.time() - _last_request_at >= _ON_DEMAND_IDLE_UNLOAD_S:
                await asyncio.to_thread(_unload_model_sync)


'''

_OLD_LIFESPAN = '''@asynccontextmanager
async def lifespan(app: FastAPI):
    global _INFER_SEM
    # One inference at a time: CUDA-graph captures are not re-entrant.
    _INFER_SEM = asyncio.Semaphore(1)
    if _METRICS_AVAILABLE:
        _M_MODEL_READY.set(0)
    # Load model off the event loop so uvicorn stays responsive.
    await asyncio.to_thread(_load_model_sync, _DEVICE, _DEVICE == "cuda")
    if _METRICS_AVAILABLE:
        backend = "faster-qwen3-tts" if _FASTER_TTS else "qwen_tts"
        _M_BACKEND_INFO.labels(device=_DEVICE, backend=backend, model_id=MODEL_ID).set(1)
        _M_MODEL_READY.set(1 if _model_ready else 0)
    yield
    # Cleanup on shutdown (releases GPU memory for clean container stop).
    global _model
    _model = None
    if _METRICS_AVAILABLE:
        _M_MODEL_READY.set(0)
    if _DEVICE == "cuda":
        torch.cuda.empty_cache()'''

_NEW_LIFESPAN = '''@asynccontextmanager
async def lifespan(app: FastAPI):
    global _INFER_SEM, _LOAD_LOCK, _last_request_at
    # One inference at a time: CUDA-graph captures are not re-entrant.
    _INFER_SEM = asyncio.Semaphore(1)
    _LOAD_LOCK = asyncio.Lock()
    _last_request_at = time.time()
    if _METRICS_AVAILABLE:
        _M_MODEL_READY.set(0)
    if _ON_DEMAND_LAZY:
        logger.info("On-demand mode: starting unloaded; the first request loads the model.")
    else:
        # Load model off the event loop so uvicorn stays responsive.
        await asyncio.to_thread(_load_model_sync, _DEVICE, _DEVICE == "cuda")
    if _METRICS_AVAILABLE:
        backend = "faster-qwen3-tts" if _FASTER_TTS else "qwen_tts"
        _M_BACKEND_INFO.labels(device=_DEVICE, backend=backend, model_id=MODEL_ID).set(1)
        _M_MODEL_READY.set(1 if _model_ready else 0)
    idle_task = asyncio.create_task(_idle_unload_loop()) if _ON_DEMAND_IDLE_UNLOAD_S > 0 else None
    yield
    if idle_task is not None:
        idle_task.cancel()
    # Cleanup on shutdown (releases GPU memory for clean container stop).
    global _model
    _model = None
    if _METRICS_AVAILABLE:
        _M_MODEL_READY.set(0)
    if _DEVICE == "cuda":
        torch.cuda.empty_cache()'''

_OLD_GUARD = '''    if not _model_ready:
        raise HTTPException(503, "Model is still loading. Check /health and retry.")'''

_NEW_GUARD = '''    await _ensure_loaded()'''


def patch(server_py: Path) -> None:
    replace_once(server_py, _OLD_LIFESPAN, _NEW_LIFESPAN)
    # Insert the helpers right before the (now patched) lifespan function.
    replace_once(server_py, _NEW_LIFESPAN, _HELPERS + _NEW_LIFESPAN)
    # Also covers POST /v1/voices (identical guard text) — registering a clone needs
    # the model bound too. The two /v1/models* listing endpoints use different text
    # and are intentionally left alone (listing doesn't need the model loaded).
    replace_n(server_py, _OLD_GUARD, _NEW_GUARD, 4)
    py_compile.compile(str(server_py), doraise=True)


if __name__ == "__main__":
    patch(Path("/app/server.py"))
