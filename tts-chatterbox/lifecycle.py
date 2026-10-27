"""One-process ASGI lifecycle: lazily load speech models and release after use."""
import asyncio

import anyio
import json
import logging

logger = logging.getLogger(__name__)
SPEECH_PATHS = frozenset({"/tts", "/v1/audio/speech"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def _complete_sync(function):
    """Keep ownership until a worker finishes, even if its request is cancelled."""
    # Starlette cancels streams through an AnyIO task-group scope. Shield that
    # level-triggered cancellation while waiting, or every await is cancelled
    # again and a busy loop prevents the executor completion from being handled.
    cancelled = False
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(asyncio.to_thread(function))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Direct asyncio task cancellation still pierces an AnyIO scope.
                cancelled = True
        result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    # Deliver ancestor scope cancellation only after the worker has finished.
    await anyio.lowlevel.checkpoint_if_cancelled()
    return result


class ModelLifecycle:
    """Wrap the complete ASGI app, including streaming and background work.

    Uvicorn must run one worker. There is no wait queue: overlapping mutations
    receive 429, while read-only health and discovery remain available.
    """

    def __init__(self, app, engine):
        self.app = app
        self.engine = engine
        self.busy = False

    def _unload(self):
        # Upstream unload collects Python garbage and empties CUDA/MPS caches,
        # but does not release the model-specific conditioning tensors itself.
        self.engine._conds_cache.clear()
        if not self.engine.unload_model():
            raise RuntimeError("Model unload failed")

    @staticmethod
    async def _error(send, status, detail):
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", b"1"),
        ]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in MUTATING_METHODS:
            await self.app(scope, receive, send)
            return
        # No await between checking and setting ownership on the event loop.
        if self.busy:
            await self._error(send, 429, "Model is busy; retry after the current request")
            return
        self.busy = True
        try:
            if scope["path"].rstrip("/") in SPEECH_PATHS:
                try:
                    loaded = await _complete_sync(self.engine.load_model)
                except Exception:
                    logger.exception("Lazy model load failed")
                    loaded = False
                if not loaded:
                    await self._error(send, 503, "Model failed to load; retry the request")
                    return
            else:
                # Manual /api/unload must also release conditioning references
                # before its own invocation of upstream unload_model().
                self.engine._conds_cache.clear()
            await self.app(scope, receive, send)
        finally:
            try:
                await _complete_sync(self._unload)
            finally:
                self.busy = False
