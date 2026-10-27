"""CPU-only listener supervisor; each Uvicorn worker owns one model lifetime.

Run as: python tts_runtime.py --app server:app --port 8004
The app calls request_worker_recycle() after its completed ASGI request cleanup.
"""

import argparse
import importlib
import os
import signal
import socket
import subprocess
import sys
import time
from collections import deque


_worker_server = None
_recycle_fd = None


class RecycleAfterRequest:
    """Reserve one mutating request, then drain after its entire ASGI call.

    Reads and lifespan events pass through. A reserved worker rejects further
    protected writes even after the response body has finished streaming.
    """

    def __init__(self, app, paths=None):
        self.app = app
        self.paths = None if paths is None else frozenset(path.rstrip("/") for path in paths)
        self.busy = False

    async def __call__(self, scope, receive, send):
        protected = (
            scope["type"] == "http"
            and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
            and (self.paths is None or scope["path"].rstrip("/") in self.paths)
        )
        if not protected:
            return await self.app(scope, receive, send)
        if self.busy:
            body = b'{"detail":"Worker is busy or recycling; retry shortly"}'
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode()),
                                    (b"connection", b"close"),
                                    (b"retry-after", b"1")]})
            await send({"type": "http.response.body", "body": body})
            return
        self.busy = True

        async def close_after_response(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = [(name, value) for name, value in message.get("headers", [])
                           if name.lower() != b"connection"]
                message["headers"] = headers + [(b"connection", b"close")]
            await send(message)

        try:
            await self.app(scope, receive, close_after_response)
        finally:
            request_worker_recycle()


def request_worker_recycle():
    """Ask the current worker to drain and exit; return whether it was running."""
    global _recycle_fd
    if _worker_server is None:
        return False
    _worker_server.should_exit = True
    if _recycle_fd is not None:
        try:
            os.write(_recycle_fd, b"R")
        except OSError:
            pass  # The supervisor may already have exited.
        finally:
            os.close(_recycle_fd)
            _recycle_fd = None
    return True


def _worker(args):
    # Apps importing this module must see the same server as this script.
    sys.modules["tts_runtime"] = sys.modules[__name__]
    global _recycle_fd
    _recycle_fd = args.recycle_fd
    import uvicorn

    module, separator, attribute = args.app.partition(":")
    if not separator or not attribute:
        raise ValueError("--app must be module:attribute")
    app = importlib.import_module(module)
    for part in attribute.split("."):
        app = getattr(app, part)
    global _worker_server
    _worker_server = uvicorn.Server(uvicorn.Config(
        app, fd=args.fd, workers=1, lifespan="on",
        timeout_graceful_shutdown=args.shutdown_timeout,
    ))
    _worker_server.run()


def _listen(host, port):
    failures = []
    for family, socktype, proto, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    ):
        listener = socket.socket(family, socktype, proto)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
            listener.listen(128)
            return listener
        except OSError as error:
            listener.close()
            failures.append(error)
    raise failures[-1] if failures else OSError("no listening address")


def _supervise(args):
    stopping = False
    child = None

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    recent_starts = deque()
    failures = 0
    with _listen(args.host, args.port) as listener:
        print(f"tts_runtime: listening on {listener.getsockname()}",
              file=sys.stderr, flush=True)
        command = [sys.executable, os.path.abspath(__file__), "--worker",
                   "--app", args.app, "--fd", str(listener.fileno()),
                   "--shutdown-timeout", str(args.shutdown_timeout)]
        while not stopping:
            # A broken app that exits successfully must not create a spawn storm.
            now = time.monotonic()
            while recent_starts and now - recent_starts[0] >= 10:
                recent_starts.popleft()
            delay = max(0, 10 - (now - recent_starts[0])) if len(recent_starts) >= 5 else 0
            if failures:
                delay = max(delay, min(5, 0.25 * 2 ** min(failures - 1, 5)))
            until = now + delay
            while not stopping and time.monotonic() < until:
                time.sleep(min(0.1, max(0, until - time.monotonic())))
            if stopping:
                break
            started = time.monotonic()
            recent_starts.append(started)
            recycle_read, recycle_write = os.pipe()
            os.set_blocking(recycle_read, False)
            try:
                try:
                    child = subprocess.Popen(
                        command + ["--recycle-fd", str(recycle_write)],
                        pass_fds=(listener.fileno(), recycle_write),
                    )
                finally:
                    os.close(recycle_write)
                # A signal may arrive between the loop check and assigning child.
                if stopping and child.poll() is None:
                    child.terminate()
                deadline = None
                while child.poll() is None:
                    try:
                        recycling = bool(os.read(recycle_read, 1))
                    except BlockingIOError:
                        recycling = False
                    if (stopping or recycling) and deadline is None:
                        deadline = time.monotonic() + args.shutdown_timeout
                    if deadline is not None and time.monotonic() >= deadline:
                        print("tts_runtime: killing worker after shutdown timeout",
                              file=sys.stderr, flush=True)
                        child.kill()
                        break
                    time.sleep(0.05)
                code = child.wait()
            finally:
                os.close(recycle_read)
            child = None
            if not stopping:
                if code == 0:
                    failures = 0
                else:
                    failures = 1 if time.monotonic() - started >= 30 else failures + 1
                    print(f"tts_runtime: worker exited {code}; restarting",
                          file=sys.stderr, flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8004)
    parser.add_argument("--shutdown-timeout", type=float, default=30)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--recycle-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.shutdown_timeout <= 0:
        parser.error("--shutdown-timeout must be positive")
    if args.worker:
        if args.fd is None:
            parser.error("--worker requires --fd")
        _worker(args)
        return 0
    return _supervise(args)


if __name__ == "__main__":
    sys.exit(main())
