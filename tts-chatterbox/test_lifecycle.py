"""CPU-only lifecycle checks: python -m unittest -v test_lifecycle.py."""
import asyncio
import ast
from types import SimpleNamespace
from pathlib import Path
import tempfile
import threading
import unittest

import anyio
from starlette.responses import StreamingResponse

from lifecycle import ModelLifecycle, _complete_sync
from patch_lifecycle import STARTUP, MODEL_INFO_OLD, MODEL_INFO_NEW, STREAM_EXECUTOR, patch


class Engine:
    def __init__(self):
        self.MODEL_LOADED = False
        self._conds_cache = {}
        self.loads = 0
        self.unloads = 0
        self.fail_load = False
        self.raise_load = False

    def load_model(self):
        self.loads += 1
        self._conds_cache['partial'] = object()
        if self.raise_load:
            raise RuntimeError('load error')
        self.MODEL_LOADED = not self.fail_load
        return self.MODEL_LOADED

    def unload_model(self):
        assert not self._conds_cache, 'Cache must be cleared before unload'
        self.MODEL_LOADED = False
        self.unloads += 1
        return True


async def response(send, body=b'audio'):
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': body})


async def request(app, path='/tts', method='POST', send=None):
    messages = []

    async def capture(message):
        messages.append(message)
        if send:
            await send(message)

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    await app({'type': 'http', 'path': path, 'method': method}, receive, capture)
    return messages


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = Engine()

    def wrap(self, app):
        return ModelLifecycle(app, self.engine)

    async def test_repeated_speech_routes(self):
        async def app(scope, receive, send):
            self.assertTrue(self.engine.MODEL_LOADED)
            self.engine._conds_cache['voice'] = object()
            await response(send)
        wrapped = self.wrap(app)
        for path in ['/tts', '/v1/audio/speech', '/tts']:
            self.assertEqual((await request(wrapped, path))[0]['status'], 200)
            self.assertFalse(self.engine.MODEL_LOADED)
            self.assertFalse(wrapped.busy)
        self.assertEqual((self.engine.loads, self.engine.unloads), (3, 3))

    async def test_error_unwinds_before_unload(self):
        events = []
        async def app(scope, receive, send):
            try:
                raise ValueError('synthesis failed')
            finally:
                self.assertTrue(self.engine.MODEL_LOADED)
                events.append('unwound')
        wrapped = self.wrap(app)
        with self.assertRaisesRegex(ValueError, 'synthesis failed'):
            await request(wrapped)
        self.assertEqual(events, ['unwound'])
        self.assertFalse(self.engine.MODEL_LOADED)
        self.assertFalse(wrapped.busy)

    async def test_stream_background_and_concurrent_routes(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            if scope['method'] == 'GET':
                await response(send, b'health')
                return
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
            started.set()
            await finish.wait()
            self.assertTrue(self.engine.MODEL_LOADED)
            await send({'type': 'http.response.body', 'body': b'last'})
            # ASGI call remains active during Starlette background work too.
            await asyncio.sleep(0)
            self.assertTrue(self.engine.MODEL_LOADED)
        wrapped = self.wrap(app)
        active = asyncio.create_task(request(wrapped))
        await started.wait()
        for path in ['/tts', '/v1/audio/speech', '/api/unload', '/restart_server',
                     '/save_settings', '/reset_settings', '/upload_reference']:
            self.assertEqual((await request(wrapped, path))[0]['status'], 429)
        self.assertEqual((await request(wrapped, '/api/model-info', 'GET'))[0]['status'], 200)
        self.assertEqual(self.engine.unloads, 0)
        finish.set()
        await active
        self.assertFalse(self.engine.MODEL_LOADED)
        self.assertEqual(self.engine.unloads, 1)

    async def test_stream_send_failure(self):
        async def app(scope, receive, send):
            await response(send)
        async def send(message):
            if message['type'] == 'http.response.body':
                raise ConnectionError('client gone')
        wrapped = self.wrap(app)
        with self.assertRaises(ConnectionError):
            await request(wrapped, send=send)
        self.assertFalse(self.engine.MODEL_LOADED)
        self.assertFalse(wrapped.busy)

    async def test_stream_generator_failure(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
            raise RuntimeError('stream generation failed')
        wrapped = self.wrap(app)
        with self.assertRaisesRegex(RuntimeError, 'stream generation'):
            await request(wrapped)
        self.assertFalse(self.engine.MODEL_LOADED)
        self.assertFalse(wrapped.busy)

    async def test_load_failure_retry(self):
        async def app(scope, receive, send):
            await response(send)
        wrapped = self.wrap(app)
        for raised in (False, True):
            self.engine.fail_load = True
            self.engine.raise_load = raised
            with self.assertLogs('lifecycle', level='ERROR') if raised else _null_context():
                self.assertEqual((await request(wrapped))[0]['status'], 503)
            self.assertFalse(self.engine._conds_cache)
            self.assertFalse(wrapped.busy)
        self.engine.fail_load = self.engine.raise_load = False
        self.assertEqual((await request(wrapped))[0]['status'], 200)
        self.assertEqual((self.engine.loads, self.engine.unloads), (3, 3))

    async def test_idle_health_does_not_load(self):
        async def app(scope, receive, send):
            self.assertFalse(self.engine.MODEL_LOADED)
            await response(send, b'{"loaded": false}')
        await request(self.wrap(app), '/api/model-info', 'GET')
        self.assertEqual((self.engine.loads, self.engine.unloads), (0, 0))

    async def test_manual_model_routes_always_return_to_idle(self):
        async def app(scope, receive, send):
            if scope['path'] == '/api/unload':
                self.engine.unload_model()
            elif scope['path'] == '/restart_server':
                self.engine.load_model()
            await response(send)
        wrapped = self.wrap(app)
        for path in ['/restart_server', '/api/unload', '/save_settings', '/reset_settings']:
            self.engine._conds_cache['old'] = object()
            await request(wrapped, path)
            self.assertFalse(self.engine.MODEL_LOADED)
            self.assertFalse(self.engine._conds_cache)

    async def test_anyio_stream_disconnect_waits_for_synthesis_before_cleanup(self):
        synth_started, disconnected = asyncio.Event(), asyncio.Event()
        finish = threading.Event()
        loop = asyncio.get_running_loop()
        recycled = False
        worker_finished = False
        def synthesize():
            nonlocal worker_finished
            loop.call_soon_threadsafe(synth_started.set)
            if not finish.wait(timeout=5):
                raise TimeoutError('test did not release synthesis')
            worker_finished = True
            return b'audio'
        async def stream():
            yield await _complete_sync(synthesize)
        async def app(scope, receive, send):
            await StreamingResponse(stream())(scope, receive, send)
        wrapped = self.wrap(app)
        async def recycle_wrapper(scope, receive, send):
            nonlocal recycled
            try:
                await wrapped(scope, receive, send)
            finally:
                self.assertTrue(worker_finished)
                self.assertFalse(self.engine.MODEL_LOADED)
                recycled = True
        async def receive():
            await disconnected.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            pass
        async def request_stream():
            await recycle_wrapper({'type': 'http', 'path': '/tts', 'method': 'POST',
                                   'asgi': {'spec_version': '2.3'}}, receive, send)
        async with anyio.create_task_group() as group:
            group.start_soon(request_stream)
            await asyncio.wait_for(synth_started.wait(), timeout=5)
            disconnected.set()
            try:
                # A timer must keep running while Starlette repeatedly cancels
                # its stream task; the shield must not busy-loop on cancellation.
                await asyncio.sleep(0.03)
                self.assertTrue(wrapped.busy)
                self.assertTrue(self.engine.MODEL_LOADED)
                self.assertEqual(self.engine.unloads, 0)
                self.assertFalse(recycled)
                self.assertFalse(worker_finished)
            finally:
                finish.set()
        self.assertTrue(worker_finished)
        self.assertTrue(recycled)
        self.assertFalse(wrapped.busy)
        self.assertEqual(self.engine.unloads, 1)

    async def test_cancelled_request_unloads(self):
        started = asyncio.Event()
        async def app(scope, receive, send):
            started.set()
            await asyncio.Event().wait()
        wrapped = self.wrap(app)
        task = asyncio.create_task(request(wrapped))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(wrapped.busy)
        self.assertFalse(self.engine.MODEL_LOADED)

    async def test_cancel_during_load_keeps_ownership_until_worker_exits(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.engine.load_model
        def load():
            entered.set()
            finish.wait(timeout=5)
            return original()
        self.engine.load_model = load
        async def app(scope, receive, send):
            self.fail('Cancelled load must not reach speech handler')
        wrapped = self.wrap(app)
        task = asyncio.create_task(request(wrapped))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(wrapped.busy)
        self.assertEqual((await request(wrapped, '/api/unload'))[0]['status'], 429)
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(wrapped.busy)
        self.assertFalse(self.engine.MODEL_LOADED)
        self.assertEqual(self.engine.unloads, 1)


from contextlib import nullcontext as _null_context


class PatchTests(unittest.TestCase):
    def test_idle_and_loaded_discovery_without_model_loading(self):
        namespace = {
            'MODEL_LOADED': False, 'loaded_model_type': None,
            'loaded_model_class_name': None, 'model_device': None,
            'chatterbox_model': None, 'TURBO_AVAILABLE': True,
            'MULTILINGUAL_AVAILABLE': True,
            'MODEL_SELECTOR_MAP': {'chatterbox-multilingual': 'multilingual',
                                   'turbo': 'turbo', 'original': 'original'},
            'SUPPORTED_LANGUAGES': {'en': 'English', 'zh': 'Chinese', 'ja': 'Japanese'},
            'TURBO_PARALINGUISTIC_TAGS': ['laugh'],
        }
        selector = ['chatterbox-multilingual']
        namespace['config_manager'] = SimpleNamespace(get_string=lambda *args: selector[0])
        # Execute only the checked patched discovery function, without engine
        # imports, torch, model construction or network access.
        tree = ast.parse(MODEL_INFO_NEW)
        self.assertEqual(len(tree.body), 1)
        exec(compile(tree, '<patched-engine-model-info>', 'exec'), namespace)
        info = namespace['get_model_info']()
        self.assertFalse(info['loaded'])
        self.assertIsNone(info['device'])
        self.assertIsNone(info['sample_rate'])
        self.assertIsNone(info['class_name'])
        self.assertEqual(info['metadata_source'], 'configured')
        self.assertEqual(info['type'], 'multilingual')
        self.assertTrue(info['supports_multilingual'])
        self.assertEqual(info['supported_languages'], namespace['SUPPORTED_LANGUAGES'])
        for value, expected in [('turbo', 'turbo'), ('original', 'original'), ('unknown', 'original')]:
            selector[0] = value
            info = namespace['get_model_info']()
            self.assertEqual(info['type'], expected)
            self.assertEqual(info['supports_paralinguistic_tags'], expected == 'turbo')
        namespace.update(MODEL_LOADED=True, loaded_model_type='multilingual',
                         loaded_model_class_name='ChatterboxMultilingualTTS',
                         model_device='cuda', chatterbox_model=SimpleNamespace(sr=24000))
        info = namespace['get_model_info']()
        self.assertTrue(info['loaded'])
        self.assertEqual(info['metadata_source'], 'loaded')
        self.assertEqual(info['type'], 'multilingual')
        self.assertEqual(info['device'], 'cuda')
        self.assertEqual(info['sample_rate'], 24000)

    def test_metadata_snapshot_during_unload(self):
        namespace = {
            'MODEL_LOADED': True, 'loaded_model_type': 'multilingual',
            'loaded_model_class_name': 'ChatterboxMultilingualTTS', 'model_device': 'cuda',
            'TURBO_AVAILABLE': True, 'MULTILINGUAL_AVAILABLE': True,
            'MODEL_SELECTOR_MAP': {'multilingual': 'multilingual'},
            'SUPPORTED_LANGUAGES': {'en': 'English', 'zh': 'Chinese'},
            'TURBO_PARALINGUISTIC_TAGS': [],
            'config_manager': SimpleNamespace(get_string=lambda *args: 'multilingual'),
        }
        class Model:
            @property
            def sr(self):
                # Force unload after the function takes its snapshot.
                namespace.update(MODEL_LOADED=False, chatterbox_model=None,
                                 loaded_model_type=None, loaded_model_class_name=None,
                                 model_device=None)
                return 24000
        namespace['chatterbox_model'] = Model()
        exec(compile(ast.parse(MODEL_INFO_NEW), '<patched-engine-model-info>', 'exec'), namespace)
        info = namespace['get_model_info']()
        self.assertTrue(info['loaded'])
        self.assertEqual((info['type'], info['device'], info['sample_rate']),
                         ('multilingual', 'cuda', 24000))
        # Every incomplete intermediate combination must produce coherent idle
        # metadata; the GPU model itself is held only for this function call.
        for field, value in [('chatterbox_model', None), ('model_device', None),
                             ('loaded_model_type', None), ('loaded_model_class_name', None),
                             ('MODEL_LOADED', False)]:
            namespace.update(MODEL_LOADED=True, chatterbox_model=SimpleNamespace(sr=24000),
                             loaded_model_type='multilingual',
                             loaded_model_class_name='ChatterboxMultilingualTTS', model_device='cuda')
            namespace[field] = value
            info = namespace['get_model_info']()
            self.assertFalse(info['loaded'])
            self.assertIsNone(info['device'])
            self.assertIsNone(info['sample_rate'])
            self.assertEqual(info['metadata_source'], 'configured')
            self.assertTrue(info['supports_multilingual'])

    def test_pinned_patch_and_reject_second_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / 'server.py'
            routes = {'/tts': 'custom_tts_endpoint', '/v1/audio/speech': 'openai_speech_endpoint',
                      '/save_settings': 'save_settings_endpoint', '/reset_settings': 'reset_settings_endpoint',
                      '/restart_server': 'restart_server_endpoint', '/api/unload': 'unload_model_endpoint'}
            fixture = 'async def lifespan(app):\n    try:\n' + STARTUP + '        yield\n    finally:\n        pass\n'
            fixture += ''.join(f'@app.post({path!r})\nasync def {name}():\n    pass\n' for path, name in routes.items())
            fixture += ('async def streaming_fixture():\n'
                        '    if True:\n        if True:\n            if True:\n'
                        + STREAM_EXECUTOR + '\n                        text=c,\n                    ),\n                )\n')
            server.write_text(fixture + '# --- Main Execution ---\n')
            engine_fixture = MODEL_INFO_OLD
            for name in ('unload_model', 'reload_model'):
                engine_fixture += (f'\ndef {name}():\n    global chatterbox_model\n'
                                   '    if chatterbox_model is not None:\n'
                                   '        del chatterbox_model\n'
                                   '        chatterbox_model = None\n')
            (root / 'engine.py').write_text(engine_fixture)
            patch(root)
            source = server.read_text()
            self.assertIn('app = ModelLifecycle(app, engine)', source)
            self.assertIn('app = RecycleAfterRequest(app)', source)
            self.assertNotIn('await loop.run_in_executor(', source)
            self.assertIn('await _complete_sync(', source)
            engine_tree = ast.parse((root / 'engine.py').read_text())
            self.assertFalse(any(isinstance(node, ast.Delete) for node in ast.walk(engine_tree)))
            self.assertNotIn('if not engine.load_model():', source)
            with self.assertRaisesRegex(RuntimeError, 'no longer matches'):
                patch(root)


if __name__ == '__main__':
    unittest.main()
