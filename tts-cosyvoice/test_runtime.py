"""Stdlib tests; fake Uvicorn exercises real processes and inherited TCP FDs."""

import asyncio
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

import tts_runtime


FAKE_UVICORN = '''
import os, signal, socket, time
class Config:
    def __init__(self, app, **kwargs):
        self.app = app
        self.__dict__.update(kwargs)
class Server:
    def __init__(self, config):
        self.config = config
        self.should_exit = False
    def run(self):
        def stop(*args):
            self.should_exit = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        listener = socket.socket(fileno=os.dup(self.config.fd))
        listener.settimeout(.05)
        while not self.should_exit:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            with conn:
                path = conn.recv(4096).split()[1].decode()
                if path == '/hang':
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                body = self.config.app(path)
                conn.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: ' +
                             str(len(body)).encode() + b'\\r\\nConnection: close\\r\\n\\r\\n' + body)
            if path in {'/hang', '/recycle-hang'}:
                while True:
                    time.sleep(1)
        # Leave a replacement gap to exercise the supervisor-owned backlog.
        time.sleep(.3)
        listener.close()
'''

FAKE_APP = '''
import os
from tts_runtime import request_worker_recycle
with open(os.environ['IMPORT_LOG'], 'a') as log:
    log.write(str(os.getpid()) + '\\n')
if 'EXIT_ON_IMPORT' in os.environ:
    os._exit(int(os.environ['EXIT_ON_IMPORT']))
def app(path):
    if path in {'/recycle', '/recycle-hang'}:
        assert request_worker_recycle(), 'callback module identity is split'
    return str(os.getpid()).encode()
'''


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root / 'uvicorn.py').write_text(textwrap.dedent(FAKE_UVICORN))
        (root / 'fake_app.py').write_text(textwrap.dedent(FAKE_APP))
        self.import_log = root / 'imports'
        env = dict(os.environ, PYTHONPATH=str(root), IMPORT_LOG=str(self.import_log))
        exit_modes = {'test_rapid_clean_exits_are_rate_limited': '0',
                      'test_unexpected_exits_use_backoff': '2'}
        if self._testMethodName in exit_modes:
            env['EXIT_ON_IMPORT'] = exit_modes[self._testMethodName]
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(tts_runtime.__file__)), '--app',
             'fake_app:app', '--host', '127.0.0.1', '--port', '0',
             '--shutdown-timeout', '0.6'],
            env=env, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
        )
        self.addCleanup(self.cleanup_process)
        ready, _, _ = select.select([self.proc.stderr], [], [], 5)
        self.assertTrue(ready, 'supervisor did not bind listener')
        line = self.proc.stderr.readline().decode()
        self.assertIn('listening on', line)
        self.port = int(line.rsplit(',', 1)[1].strip().rstrip(')'))

    def cleanup_process(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.proc.stderr.close()

    def request(self, path='/'):
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as conn:
            conn.sendall(f'GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
            data = b''
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertTrue(data.startswith(b'HTTP/1.1 200'))
        return int(data.split(b'\r\n\r\n')[1])

    def test_listener_and_queued_connection_survive_recycle(self):
        first = self.request('/recycle')
        # Connect immediately, while the old worker is draining for .3 seconds.
        second = self.request('/recycle')
        third = self.request()
        self.assertEqual(len({first, second, third}), 3)
        self.assertIsNone(self.proc.poll())
        imports = [int(pid) for pid in self.import_log.read_text().splitlines()]
        self.assertEqual(imports, [first, second, third])
        self.assertNotIn(self.proc.pid, imports)

    def test_stop_does_not_restart_worker(self):
        child = self.request()
        self.proc.send_signal(signal.SIGINT)
        self.assertEqual(self.proc.wait(timeout=3), 0)
        self.assertEqual(self.import_log.read_text().splitlines(), [str(child)])
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=.3)

    def test_hung_worker_is_killed_within_shutdown_bound(self):
        child = self.request('/hang')
        started = time.monotonic()
        self.proc.terminate()
        self.assertEqual(self.proc.wait(timeout=3), 0)
        self.assertLess(time.monotonic() - started, 2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_recycle_also_kills_hung_worker_and_replaces_it(self):
        child = self.request('/recycle-hang')
        started = time.monotonic()
        replacement = self.request()
        self.assertNotEqual(child, replacement)
        self.assertLess(time.monotonic() - started, 3)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_rapid_clean_exits_are_rate_limited(self):
        time.sleep(.8)
        imports = self.import_log.read_text().splitlines()
        self.assertGreaterEqual(len(imports), 1)
        self.assertLessEqual(len(imports), 5)
        self.assertIsNone(self.proc.poll())
        self.proc.terminate()
        self.assertEqual(self.proc.wait(timeout=3), 0)
        self.assertEqual(self.import_log.read_text().splitlines(), imports)

    def test_unexpected_exits_use_backoff(self):
        time.sleep(.7)
        imports = self.import_log.read_text().splitlines()
        self.assertGreaterEqual(len(imports), 1)
        self.assertLessEqual(len(imports), 3)
        self.assertIsNone(self.proc.poll())


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_cleanup_finishes_before_recycle_and_writes_stay_busy(self):
        streaming = asyncio.Event()
        finish = asyncio.Event()
        cleaned = []
        events = []

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200})
            await send({'type': 'http.response.body', 'body': b'chunk', 'more_body': True})
            streaming.set()
            await finish.wait()
            await send({'type': 'http.response.body', 'body': b'end'})
            # Models background task and generator cleanup after final body.
            await asyncio.sleep(0)
            cleaned.append(True)

        async def send(event):
            events.append(event)

        middleware = tts_runtime.RecycleAfterRequest(app, paths={'/speech'})
        scope = {'type': 'http', 'method': 'POST', 'path': '/speech/'}
        with patch.object(tts_runtime, 'request_worker_recycle') as recycle:
            first = asyncio.create_task(middleware(scope, None, send))
            await streaming.wait()
            self.assertIn((b'connection', b'close'), events[0]['headers'])
            await middleware(scope, None, send)
            self.assertEqual(events[-2]['status'], 429)
            self.assertIn((b'connection', b'close'), events[-2]['headers'])
            recycle.assert_not_called()
            finish.set()
            await first
            self.assertEqual(cleaned, [True])
            recycle.assert_called_once_with()
            await middleware(scope, None, send)
            self.assertEqual(events[-2]['status'], 429)
            recycle.assert_called_once_with()

    async def test_reads_lifespan_and_unconfigured_paths_pass_through(self):
        calls = []

        async def app(scope, receive, send):
            calls.append(scope)

        middleware = tts_runtime.RecycleAfterRequest(app, paths={'/speech'})
        scopes = [{'type': 'lifespan'},
                  {'type': 'http', 'method': 'GET', 'path': '/speech'},
                  {'type': 'http', 'method': 'POST', 'path': '/other'}]
        with patch.object(tts_runtime, 'request_worker_recycle') as recycle:
            for scope in scopes:
                await middleware(scope, None, None)
            recycle.assert_not_called()
        self.assertEqual(calls, scopes)
        self.assertFalse(middleware.busy)

    async def test_app_failure_still_recycles(self):
        async def app(scope, receive, send):
            raise RuntimeError('request failed')

        middleware = tts_runtime.RecycleAfterRequest(app)
        with patch.object(tts_runtime, 'request_worker_recycle') as recycle:
            with self.assertRaisesRegex(RuntimeError, 'request failed'):
                await middleware({'type': 'http', 'method': 'POST', 'path': '/'}, None, None)
            recycle.assert_called_once_with()
        self.assertTrue(middleware.busy)


if __name__ == '__main__':
    unittest.main()
