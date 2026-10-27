"""Bounded narration chunks, reference containment and GPU serialization."""
import asyncio
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
import weakref
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import HTTPException

spec = importlib.util.spec_from_file_location('server', Path(__file__).with_name('server.py'))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class AdapterTests(unittest.TestCase):
    def test_chunking_keeps_content_and_bounds(self):
        for text in ['First sentence. Second sentence!', '剑光落下。旧事未散！' * 100,
                     '甲' * 900, 'a long sentence without punctuation ' * 90,
                     'First\nsecond\n\nthird.', 'No final punctuation']:
            chunks = list(server.chunks(text))
            self.assertTrue(chunks)
            self.assertTrue(all(0 < len(c) <= 240 for c in chunks))
            self.assertEqual(''.join(''.join(chunks).split()), ''.join(text.split()))

    def test_complete_sentences(self):
        self.assertEqual(list(server.chunks('Hello there. Welcome home!', 20)),
                         ['Hello there.', 'Welcome home!'])

    def test_reference_path_and_duration(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'VOICE_ROOT', Path(folder)):
            sf.write(Path(folder) / 'good.wav', np.ones(32000) * .1, 16000)
            self.assertEqual(server.reference('good').name, 'good.wav')
            for name in ['../secret', '/tmp/ref.wav', 'http://example.com/ref.wav', 'missing']:
                with self.assertRaises(HTTPException):
                    server.reference(name)
            sf.write(Path(folder) / 'short.wav', np.ones(100), 16000)
            with self.assertRaises(HTTPException):
                server.reference('short')

    def test_busy_refuses_second_request(self):
        request = server.SpeechRequest(input='Hello.', voice='good', prompt_text='Reference.')
        with patch.object(server, 'server_ready', True), patch.object(server, 'load_model') as load, patch.object(server, 'reference', return_value=Path('/cache/voices/good.wav')):
            server.lock.acquire()
            try:
                with self.assertRaises(HTTPException) as error:
                    server.speech(request)
                self.assertEqual(error.exception.status_code, 429)
                load.assert_not_called()
            finally:
                server.lock.release()

    def test_health_ready_without_loading(self):
        with patch.object(server, 'server_ready', True), patch.object(server, 'model', None), patch.object(server, 'load_model') as load:
            self.assertEqual(server.health(), {'ready': True, 'model': 'cosyvoice3',
                'device': 'cuda', 'runtime': 'rocm', 'sample_rate': None,
                'loaded': False, 'busy': False})
            server.lock.acquire()
            try:
                self.assertTrue(server.health()['busy'])
            finally:
                server.lock.release()
            load.assert_not_called()

    def test_startup_does_not_initialize_gpu(self):
        async def run():
            async with server.lifespan(server.app):
                self.assertTrue(server.health()['ready'])
                self.assertFalse(server.health()['loaded'])
            self.assertFalse(server.server_ready)
        with patch.object(server, 'model', None), patch.object(server, 'load_model') as load, \
                patch.object(server.torch.cuda, 'is_available') as available, \
                patch.object(server.torch.cuda, 'is_initialized', return_value=False), \
                patch.object(server.torch.version, 'hip', 'mock-rocm'), \
                patch.object(server.torch.cuda, 'init') as init, \
                patch.object(server.torch.cuda, 'synchronize'), \
                patch.object(server.torch.cuda, 'empty_cache'):
            asyncio.run(run())
            init.assert_not_called()
            available.assert_not_called()
            load.assert_not_called()

    def test_startup_rejects_missing_gpu(self):
        async def run():
            async with server.lifespan(server.app):
                self.fail('startup accepted missing GPU')
        with patch.object(server.torch.version, 'hip', None):
            with self.assertRaisesRegex(RuntimeError, 'ROCm GPU'):
                asyncio.run(run())

    def exercise_request(self, failure=None, response_format='wav', repeats=1):
        # Weak references verify failed generator/traceback frames are gone before
        # empty_cache runs, rather than merely checking a cleanup function call.
        refs = []
        events = []

        class Tensor:
            def detach(self):
                return self

            def float(self):
                return self

            def cpu(self):
                return self

            def numpy(self):
                return np.ones(2400, dtype=np.float32) * .1

        class Model:
            sample_rate = 24000

            def inference_zero_shot(self, *args, **kwargs):
                tensor = Tensor()
                refs.append(weakref.ref(tensor))
                try:
                    yield {'tts_speech': tensor}
                    if failure == 'inference':
                        raise RuntimeError('mock inference failure')
                finally:
                    events.append('generator_closed')

        def load():
            self.assertTrue(server.lock.locked())
            self.assertIsNone(server.model)
            server.model = Model()
            refs.append(weakref.ref(server.model))
            events.append('load')
            if failure == 'load':
                # A partial load can also leave model references in traceback locals.
                partial = server.model
                raise RuntimeError('mock partial load failure')

        def synchronize():
            self.assertTrue(server.lock.locked())
            events.append('sync')

        def empty_cache():
            self.assertTrue(server.lock.locked())
            self.assertIsNone(server.model)
            self.assertTrue(all(ref() is None for ref in refs))
            events.append('empty')

        real_write = server.sf.write

        def encode(*args, **kwargs):
            self.assertIsNotNone(server.model)
            self.assertTrue(server.lock.locked())
            if failure == 'encode':
                raise RuntimeError('mock encoding failure')
            result = real_write(*args, **kwargs)
            events.append('encoded')
            return result

        request = server.SpeechRequest(input='Hello.', voice='good', prompt_text='Reference.',
                                       response_format=response_format)
        with patch.object(server, 'server_ready', True), patch.object(server, 'model', None), \
                patch.object(server, 'load_model', side_effect=load) as loader, \
                patch.object(server, 'reference', return_value=Path('/cache/voices/good.wav')), \
                patch.object(server.sf, 'write', side_effect=encode), \
                patch.object(server.torch.cuda, 'is_initialized', return_value=True), \
                patch.object(server.torch.cuda, 'synchronize', side_effect=synchronize), \
                patch.object(server.torch.cuda, 'empty_cache', side_effect=empty_cache), \
                patch.object(server.logging, 'exception') as log:
            for _ in range(repeats):
                if failure:
                    with self.assertRaises(HTTPException) as error:
                        server.speech(request)
                    self.assertEqual(error.exception.status_code, 500)
                    # Do not retain logging's exc_info through a test mock.
                    self.assertTrue(log.called)
                else:
                    response = server.speech(request)
                    self.assertEqual(response.headers['x-audio-sample-rate'], '24000')
                    if response_format == 'wav':
                        samples, rate = sf.read(io.BytesIO(response.body))
                        self.assertEqual(rate, 24000)
                        self.assertEqual(samples.size, 2400)
                    else:
                        self.assertEqual(len(response.body), 4800)
                    self.assertLess(events.index('encoded'), events.index('sync'))
                self.assertIsNone(server.model)
                self.assertFalse(server.lock.locked())
                self.assertFalse(server.health()['loaded'])
            self.assertEqual(loader.call_count, repeats)
            self.assertEqual(events.count('empty'), repeats)

    def test_success_unloads_and_reloads_every_request(self):
        self.exercise_request(repeats=2)

    def test_raw_pcm_unloads(self):
        self.exercise_request(response_format='pcm')

    def test_failures_unload_after_frames_are_released(self):
        for failure in ('load', 'inference', 'encode'):
            with self.subTest(failure=failure):
                self.exercise_request(failure=failure)


if __name__ == '__main__':
    unittest.main()
