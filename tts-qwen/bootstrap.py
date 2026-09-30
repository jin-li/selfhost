"""Use a complete pinned cache offline, or download it before starting upstream."""
import os
import sys
from pathlib import Path
from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
REVISION = "fd4b254389122332181a7c3db7f27e918eec64e3"
REQUIRED = (
    "config.json", "generation_config.json", "merges.txt", "model.safetensors",
    "preprocessor_config.json", "tokenizer_config.json", "vocab.json",
    "speech_tokenizer/config.json", "speech_tokenizer/configuration.json",
    "speech_tokenizer/model.safetensors", "speech_tokenizer/preprocessor_config.json",
)
WEIGHT_SIZES = {"model.safetensors": 3857413744,
                "speech_tokenizer/model.safetensors": 682293092}


def complete(snapshot):
    root = Path(snapshot)
    return (all((root / name).is_file() and (root / name).stat().st_size > 0 for name in REQUIRED)
            and all((root / name).stat().st_size == size for name, size in WEIGHT_SIZES.items()))


def resolve_snapshot():
    try:
        cached = snapshot_download(MODEL, revision=REVISION, local_files_only=True)
        if complete(cached):
            return cached
    except LocalEntryNotFoundError:
        pass
    downloaded = snapshot_download(MODEL, revision=REVISION)
    if not complete(downloaded):
        raise RuntimeError("Pinned Qwen snapshot is incomplete")
    return downloaded


if __name__ == "__main__":
    os.environ["QWEN3_TTS_MODEL_ID"] = resolve_snapshot()
    os.execv(sys.executable, [sys.executable, "/app/server.py", *sys.argv[1:]])
