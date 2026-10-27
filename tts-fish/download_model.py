"""Optional bounded HTTP Range prefetch with official pinned LFS SHA256 checks.

Run while bootstrap/prefetch are stopped. Retains HF .incomplete files and range
parts on failure; publishes cache blobs only after verifying the complete hash.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import urllib.request

MODEL = 'fishaudio/s2-pro'
REVISION = '1de9996b6be38b745688de084d87a5633f714e4e'
PATTERNS = ['*.json', '*.safetensors', '*.pth', '*.jinja', 'LICENSE.md', 'README.md']
import fnmatch


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def download(cache, workers=8, transport="https://huggingface.co"):
    if transport not in ("https://huggingface.co", "https://hf-mirror.com"):
        raise ValueError("Unsupported download transport")
    metadata = fetch_json(f'https://huggingface.co/api/models/{MODEL}/revision/{REVISION}?blobs=true')
    repo = Path(cache) / 'hub' / ('models--' + MODEL.replace('/', '--'))
    snapshot = repo / 'snapshots' / REVISION
    (repo / 'blobs').mkdir(parents=True, exist_ok=True)
    for item in metadata['siblings']:
        name = item['rfilename']
        if not any(fnmatch.fnmatch(name, pattern) for pattern in PATTERNS):
            continue
        lfs = item.get('lfs')
        if not lfs:
            # These small files can use the standard pinned snapshot downloader.
            from huggingface_hub import hf_hub_download
            hf_hub_download(MODEL, name, revision=REVISION, cache_dir=str(Path(cache) / 'hub'))
            continue
        digest, size = lfs['sha256'], item['size']
        blob = repo / 'blobs' / digest
        if blob.is_file() and blob.stat().st_size == size:
            print('cached', name, flush=True)
        else:
            parts = repo / 'range-parts' / digest
            parts.mkdir(parents=True, exist_ok=True)
            partial = blob.with_name(blob.name + '.incomplete')
            prefix = min(partial.stat().st_size, size) if partial.exists() else 0
            block = 16 * 1024 * 1024
            ranges = [(start, min(start + block, size) - 1) for start in range(prefix, size, block)]

            def get_part(bounds):
                start, end = bounds
                target = parts / str(start)
                if target.is_file() and target.stat().st_size == end - start + 1:
                    return
                # Distinct resolve queries prevent intermediary range-cache collisions.
                url = f'{transport}/{MODEL}/resolve/{REVISION}/{name}?download=true&range_start={start}'
                for attempt in range(5):
                    try:
                        request = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}'})
                        with urllib.request.urlopen(request, timeout=180) as response:
                            expected = f'bytes {start}-{end}/{size}'
                            if response.status != 206 or response.headers.get('Content-Range') != expected:
                                raise RuntimeError(f'Range not honored: {response.status} {response.headers.get("Content-Range")}')
                            with target.with_suffix('.tmp').open('wb') as output:
                                shutil.copyfileobj(response, output, 1024 * 1024)
                        if target.with_suffix('.tmp').stat().st_size != end - start + 1:
                            raise RuntimeError('Incomplete range')
                        target.with_suffix('.tmp').replace(target)
                        return
                    except Exception:
                        if attempt == 4:
                            raise
                        time.sleep(2 ** attempt)

            print('fetch', name, size, 'existing prefix', prefix, 'workers', workers, flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for i, task in enumerate(as_completed([executor.submit(get_part, r) for r in ranges]), 1):
                    task.result()
                    if i % 8 == 0 or i == len(ranges):
                        print(name, i, '/', len(ranges), 'ranges', flush=True)
            assembled = parts / 'assembled'
            checksum = hashlib.sha256()
            with assembled.open('wb') as output:
                if prefix:
                    with partial.open('rb') as source:
                        remaining = prefix
                        while remaining:
                            data = source.read(min(1024 * 1024, remaining))
                            if not data:
                                raise RuntimeError('Prefix truncated')
                            output.write(data)
                            checksum.update(data)
                            remaining -= len(data)
                for start, end in ranges:
                    with (parts / str(start)).open('rb') as source:
                        while data := source.read(1024 * 1024):
                            output.write(data)
                            checksum.update(data)
            if assembled.stat().st_size != size or checksum.hexdigest() != digest:
                raise RuntimeError('Pinned SHA256 mismatch; preserved all source parts for inspection: ' + name)
            assembled.replace(blob)
            print('verified SHA256', name, digest, flush=True)
        link = snapshot / name
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(os.path.relpath(blob, link.parent))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cache', required=True, help='HF_HOME directory')
    parser.add_argument('--workers', type=int, choices=range(1, 17), default=8)
    parser.add_argument("--transport", choices=["https://huggingface.co", "https://hf-mirror.com"], default="https://huggingface.co")
    args = parser.parse_args()
    download(args.cache, args.workers, args.transport)
