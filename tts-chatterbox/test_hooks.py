"""Run inside the built image: python /tmp/test_hooks.py (no model/GPU needed)."""
import gc
from types import SimpleNamespace
import unittest
import weakref

import torch
from chatterbox.models.t3.inference.alignment_stream_analyzer import AlignmentStreamAnalyzer
from chatterbox.models.t3.t3 import _release_alignment_hooks


class HookLifetime(unittest.TestCase):
    def test_repeated_requests_and_failure_release_owned_hooks(self):
        transformer = SimpleNamespace(layers=[SimpleNamespace(self_attn=torch.nn.Identity()) for _ in range(14)])
        # Unrelated hooks must survive the cleanup.
        permanent = transformer.layers[9].self_attn.register_forward_hook(lambda *args: None)
        backend = SimpleNamespace(alignment_stream_analyzer=None)
        model = SimpleNamespace(patched_model=backend)
        references = []

        @_release_alignment_hooks
        def inference(model, fail=False):
            analyzer = AlignmentStreamAnalyzer(transformer, None, (0, 10))
            references.append(weakref.ref(analyzer))
            model.patched_model.alignment_stream_analyzer = analyzer
            self.assertEqual(sum(len(layer.self_attn._forward_hooks) for layer in transformer.layers), 4)
            if fail: raise RuntimeError('inference interrupted')
            return 'audio'

        for index in range(100):
            if index % 7 == 0:
                with self.assertRaisesRegex(RuntimeError, 'interrupted'): inference(model, True)
            else:
                self.assertEqual(inference(model), 'audio')
            self.assertIsNone(backend.alignment_stream_analyzer)
            self.assertEqual(sum(len(layer.self_attn._forward_hooks) for layer in transformer.layers), 1)
        gc.collect()
        self.assertTrue(all(reference() is None for reference in references))
        permanent.remove()


if __name__ == '__main__': unittest.main()
