"""CPU-only request-contract checks against the built adapter; no model loading."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import HTTPException
import server


class AdapterContract(unittest.TestCase):
    def reject(self, status, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            server.speech(server.Speech(input='Test speech.', **kwargs))
        self.assertEqual(caught.exception.status_code, status)

    def test_unsupported_requests(self):
        for options in ({'model': 'other'}, {'response_format': 'mp3'},
                        {'speed': 1.1}, {'voice': 'other'}, {'language': 'de'}):
            with self.subTest(options=options):
                self.reject(400, **options)

    def test_references_and_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / 'ryan-en'
            with patch.object(server, 'Path', lambda _: Path(directory)):
                self.reject(503)
                reference.with_suffix('.txt').write_text('Test reference.')
                sf.write(reference.with_suffix('.wav'), np.zeros(1000), 24000)
                self.reject(422)
                sf.write(reference.with_suffix('.wav'), np.zeros(48000), 24000)
                reference.with_suffix('.txt').write_text('   ')
                self.reject(422)
                reference.with_suffix('.txt').write_text('Test reference.')
                sf.write(reference.with_suffix('.wav'), np.full(48000, np.nan),
                         24000, subtype='FLOAT')
                self.reject(422)
                sf.write(reference.with_suffix('.wav'),
                         0.1 * np.sin(np.arange(48000) * 0.1), 24000)
                server.lock.acquire()
                try:
                    self.reject(429)
                finally:
                    server.lock.release()

    def test_inference_slot_released_after_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'probe failure'):
            with server.inference_slot():
                raise RuntimeError('probe failure')
        self.assertTrue(server.lock.acquire(blocking=False))
        server.lock.release()


class ChunkContract(unittest.TestCase):
    def check_chunks(self, text, limit=200):
        pieces = list(server.chunks(text, limit))
        self.assertEqual(''.join(pieces), text)
        self.assertTrue(all(0 < len(piece) <= limit for piece in pieces))
        return pieces

    def test_english_sentence_boundaries(self):
        self.assertEqual(self.check_chunks('Hello there. Welcome home!', 20),
                         ['Hello there.', ' Welcome home!'])
        self.check_chunks('Mr. Li left at 5.30 p.m. "Wait!" he called. ' * 20)

    def test_chinese_sentence_boundaries(self):
        pieces = self.check_chunks('剑光落下。旧事未散！' * 100)
        self.assertTrue(all(piece.endswith(('。', '！')) for piece in pieces))

    def test_long_unpunctuated_and_boundary_cases(self):
        for text in ('a' * 1001, '甲' * 1001, '🙂' * 401,
                     'word ' * 190, 'x' * 200 + ' ' + 'y' * 201):
            with self.subTest(text=text[:20]):
                self.check_chunks(text)

    def test_exact_reconstruction_including_whitespace(self):
        for text in (' First.\nsecond\n\nthird. ', '  ' * 300 + 'Last word.',
                     'No final punctuation', ''):
            self.check_chunks(text)


class SnapshotContract(unittest.TestCase):
    def prepare(self, root):
        path = root / 'models--fishaudio--s2-pro' / 'snapshots' / server.MODEL_REV
        path.mkdir(parents=True)
        for name in server.REQUIRED_FILES:
            (path / name).write_text('{}')
        for name in server.WEIGHT_SIZES:
            (path / name).write_bytes(b'x' * 16)
        (path / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': {
            'a': 'model-00001-of-00002.safetensors',
            'b': 'model-00002-of-00002.safetensors'}}))
        return path

    def test_complete_cache_uses_local_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.prepare(root)
            sizes = dict.fromkeys(server.WEIGHT_SIZES, 16)
            with patch.object(server, 'HF_HUB_CACHE', str(root)), \
                 patch.object(server, 'WEIGHT_SIZES', sizes), \
                 patch.object(server, 'snapshot_download', return_value=str(path)) as download:
                self.assertEqual(server.snapshot(), path)
                self.assertTrue(download.call_args.kwargs['local_files_only'])

    def test_incomplete_cache_fetches(self):
        for problem in ('missing-codec', 'truncated-shard', 'invalid-index'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.prepare(root)
                if problem == 'missing-codec':
                    (path / 'codec.pth').unlink()
                elif problem == 'truncated-shard':
                    (path / 'model-00001-of-00002.safetensors').write_bytes(b'x')
                else:
                    (path / 'model.safetensors.index.json').write_text('{}')
                sizes = dict.fromkeys(server.WEIGHT_SIZES, 16)
                with patch.object(server, 'HF_HUB_CACHE', str(root)), \
                     patch.object(server, 'WEIGHT_SIZES', sizes), \
                     patch.object(server, 'snapshot_download', return_value=str(path)) as download:
                    server.snapshot()
                    self.assertNotIn('local_files_only', download.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
