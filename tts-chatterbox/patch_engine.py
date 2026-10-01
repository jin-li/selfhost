"""Fix alignment-hook lifetime in the pinned multilingual Chatterbox engine."""
from pathlib import Path
import py_compile


def replace_once(path, old, new):
    source = path.read_text()
    if source.count(old) != 1:
        raise RuntimeError(f"Pinned engine patch no longer matches {path.name}")
    path.write_text(source.replace(old, new, 1))


def patch(base):
    analyzer = base / 'models/t3/inference/alignment_stream_analyzer.py'
    replace_once(analyzer, '        self.last_aligned_attns = []',
                 '        self._hook_handles = []\n        self.last_aligned_attns = []')
    replace_once(analyzer, '        target_layer.register_forward_hook(attention_forward_hook)',
                 '        self._hook_handles.append(target_layer.register_forward_hook(attention_forward_hook))')
    replace_once(analyzer, '    def step(self, logits, next_token=None):', '''    def close(self):
        """Release request-owned hooks and their retained attention tensors."""
        for handle in self._hook_handles:
            handle.remove()
        self._hook_handles.clear()
        self.last_aligned_attns.clear()
        self.alignment = None

    def step(self, logits, next_token=None):''')
    t3 = base / 'models/t3/t3.py'
    source = t3.read_text()
    marker = 'class T3('
    if source.count(marker) != 1: raise RuntimeError('Pinned T3 class no longer matches')
    start = source.index(marker)
    wrapper = '''def _release_alignment_hooks(method):
    from functools import wraps
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            backend = getattr(self, "patched_model", None)
            analyzer = getattr(backend, "alignment_stream_analyzer", None)
            if analyzer is not None:
                analyzer.close()
                backend.alignment_stream_analyzer = None
    return wrapped


'''
    t3.write_text(source[:start] + wrapper + source[start:])
    replace_once(t3, '    @torch.inference_mode()\n    def inference(',
                 '    @_release_alignment_hooks\n    @torch.inference_mode()\n    def inference(')
    for path in (analyzer, t3): py_compile.compile(str(path), doraise=True)


if __name__ == '__main__':
    import chatterbox
    patch(Path(chatterbox.__file__).parent)
