"""Download only the pinned inference snapshot; cache remains outside Git."""
import os
import sys
from pathlib import Path
from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

MODEL = 'FunAudioLLM/Fun-CosyVoice3-0.5B-2512'
REVISION = '29e01c4e8d000f4bcd70751be16fa94bf3d85a18'
WEIGHT_SIZES = {'CosyVoice-BlankEN/model.safetensors': 988097824,
                'campplus.onnx': 28303423, 'flow.pt': 1329116148,
                'hift.pt': 83202622, 'llm.pt': 2024669519,
                'speech_tokenizer_v3.onnx': 969451503}
PATTERNS = ['CosyVoice-BlankEN/*', 'campplus.onnx', 'cosyvoice3.yaml',
            'flow.pt', 'hift.pt', 'llm.pt', 'speech_tokenizer_v3.onnx']

def complete(model):
    root = Path(model)
    metadata = ['cosyvoice3.yaml', 'CosyVoice-BlankEN/config.json',
                'CosyVoice-BlankEN/tokenizer_config.json', 'CosyVoice-BlankEN/vocab.json',
                'CosyVoice-BlankEN/merges.txt']
    return (all((root / name).is_file() and (root / name).stat().st_size > 0 for name in metadata)
            and all((root / name).is_file() and (root / name).stat().st_size == size
                    for name, size in WEIGHT_SIZES.items()))


def resolve_snapshot():
    try:
        cached = snapshot_download(MODEL, revision=REVISION, allow_patterns=PATTERNS,
                                   local_files_only=True)
        if complete(cached):
            return cached
    except LocalEntryNotFoundError:
        pass
    downloaded = snapshot_download(MODEL, revision=REVISION, allow_patterns=PATTERNS)
    if not complete(downloaded):
        raise RuntimeError('Pinned CosyVoice snapshot is missing or incomplete')
    return downloaded


if __name__ == '__main__':
    model = resolve_snapshot()
    os.environ['COSYVOICE_MODEL_DIR'] = model
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.execv(sys.executable, [sys.executable, '/app/tts_runtime.py',
                            '--app', 'server:app', '--port', '8007'])
