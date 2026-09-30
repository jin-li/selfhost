"""The model bootstrap must not contact the Hub for a complete pinned cache."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bootstrap


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in bootstrap.REQUIRED:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'abc')
        self.sizes = patch.object(bootstrap, 'WEIGHT_SIZES', {name: 3 for name in bootstrap.WEIGHT_SIZES})
        self.sizes.start()
        self.addCleanup(self.sizes.stop)

    def test_complete_cache_stays_offline(self):
        with patch.object(bootstrap, 'snapshot_download', return_value=str(self.root)) as download:
            self.assertEqual(bootstrap.resolve_snapshot(), str(self.root))
            download.assert_called_once_with(bootstrap.MODEL, revision=bootstrap.REVISION, local_files_only=True)

    def test_missing_cache_is_downloaded(self):
        with patch.object(bootstrap, 'snapshot_download', side_effect=[bootstrap.LocalEntryNotFoundError('missing'), str(self.root)]) as download:
            self.assertEqual(bootstrap.resolve_snapshot(), str(self.root))
            self.assertEqual(download.call_count, 2)
            self.assertNotIn('local_files_only', download.call_args.kwargs)

    def test_incomplete_download_is_rejected(self):
        (self.root / 'model.safetensors').unlink()
        with patch.object(bootstrap, 'snapshot_download', return_value=str(self.root)) as download:
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                bootstrap.resolve_snapshot()
            self.assertEqual(download.call_count, 2)

    def test_truncated_weights_are_incomplete(self):
        (self.root / 'model.safetensors').write_bytes(b'a')
        self.assertFalse(bootstrap.complete(self.root))


if __name__ == '__main__':
    unittest.main()
