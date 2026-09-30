"""Build a single .m4b audiobook from the per-chapter MP3s of a finished job.

An .m4b is one file carrying every chapter plus a chapter index, cover art and
book metadata. Audiobookshelf, Apple Books and most players read the index
natively, so listeners get real chapter navigation and resume instead of a
folder of loose tracks.

Deliberately dependency-free beyond ffmpeg/ffprobe (already required for MP3
encoding) and importable without Flask, so it can be unit-tested on its own.

    from m4b import build_m4b
    build_m4b(Path("/data/audiobooks/Some Book_ab12"), title="Some Book",
              author="A. Writer")
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger('app')

# AAC at 64k mono is transparent for speech and roughly a third the size of the
# 192k MP3s; audiobooks are long, so the saving is real.
DEFAULT_BITRATE = '64k'
COVER_NAMES = ('cover.jpg', 'cover.jpeg', 'cover.png', 'cover.webp')


def _tool(name: str) -> str | None:
    return shutil.which(name)


def chapter_files(src_dir: Path) -> list[Path]:
    """MP3s of a finished job, in playback order.

    Files are named either "01 - Title.mp3" (after rename_output_files) or
    "001_title.mp3" (before). Both sort correctly as strings because the number
    is zero-padded and leads — so a plain sort is right, and stays right if the
    naming changes again.
    """
    return sorted((p for p in src_dir.glob('*.mp3') if p.is_file()),
                  key=lambda p: p.name.lower())


_DUR_RE = re.compile(r'Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)')


def probe_duration(path: Path) -> float:
    """Duration in seconds, or 0.0 if it genuinely can't be read.

    Prefers ffprobe, but falls back to parsing `ffmpeg -i`. Some images ship
    ffmpeg without ffprobe, and without a fallback the m4b build would silently
    never happen — the failure mode this project keeps getting bitten by.
    """
    ffprobe = _tool('ffprobe')
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'json', str(path)],
                capture_output=True, text=True, timeout=60)
            return float(json.loads(out.stdout)['format']['duration'])
        except Exception:
            pass
    ffmpeg = _tool('ffmpeg')
    if not ffmpeg:
        return 0.0
    try:
        # ffmpeg with no output writes the container header to stderr and exits
        # non-zero; that's expected, we only want the Duration line.
        out = subprocess.run([ffmpeg, '-i', str(path)],
                             capture_output=True, text=True, timeout=60)
        m = _DUR_RE.search(out.stderr or '')
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


def chapter_title(path: Path) -> str:
    """Human chapter name from the filename ("01 - The Prologue.mp3")."""
    stem = path.stem
    m = re.match(r'^\s*\d+\s*[-_]\s*(.+)$', stem)
    if m:
        stem = m.group(1)
    return re.sub(r'\s+', ' ', stem.replace('_', ' ')).strip() or path.stem


def build_ffmetadata(entries: list[tuple[str, float]], title: str = '',
                     author: str = '', extra: dict | None = None) -> str:
    """FFMETADATA1 text with one [CHAPTER] per file.

    `entries` is [(chapter_title, duration_seconds), ...] in order. Chapter
    marks are cumulative and expressed in milliseconds (TIMEBASE 1/1000).

    `extra` carries whatever else the epub gave us — year, publisher, series,
    description, language. Audiobookshelf reads these, and there is no reason
    the single-file output should be poorer than the per-chapter MP3s (#32).
    """
    lines = [';FFMETADATA1']
    if title:
        lines.append(f'title={_esc(title)}')
        lines.append(f'album={_esc(title)}')
    if author:
        lines.append(f'artist={_esc(author)}')
        lines.append(f'album_artist={_esc(author)}')
    lines.append('genre=Audiobook')
    for key, val in (extra or {}).items():
        if val:
            lines.append(f'{key}={_esc(val)}')
    start_ms = 0
    for name, dur in entries:
        end_ms = start_ms + max(1, int(round(dur * 1000)))
        lines += ['', '[CHAPTER]', 'TIMEBASE=1/1000',
                  f'START={start_ms}', f'END={end_ms}', f'title={_esc(name)}']
        start_ms = end_ms
    return '\n'.join(lines) + '\n'


def _esc(v: str) -> str:
    # ffmetadata treats these as syntax; escape so titles survive verbatim.
    return re.sub(r'([=;#\\\n])', r'\\\1', str(v))


def find_cover(src_dir: Path) -> Path | None:
    for name in COVER_NAMES:
        p = src_dir / name
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def build_m4b(src_dir: Path, out_path: Path | None = None, title: str = '',
              author: str = '', bitrate: str = DEFAULT_BITRATE,
              cover: Path | None = None, extra: dict | None = None) -> Path | None:
    """Concatenate a job's MP3s into one chaptered .m4b. Returns the path, or
    None if there is nothing to do or ffmpeg is unavailable.

    The source MP3s are left untouched — callers decide whether to keep both.
    """
    src_dir = Path(src_dir)
    ffmpeg = _tool('ffmpeg')
    if not ffmpeg:
        log.warning('m4b: ffmpeg not on PATH — skipping')
        return None
    files = chapter_files(src_dir)
    if not files:
        log.warning('m4b: no mp3s in %s', src_dir)
        return None

    title = title or src_dir.name
    out_path = Path(out_path) if out_path else src_dir / f'{title}.m4b'
    cover = cover or find_cover(src_dir)

    entries = [(chapter_title(f), probe_duration(f)) for f in files]
    if any(d <= 0 for _, d in entries):
        # Without real durations the chapter marks would be wrong, which is
        # worse than no m4b: players would seek to the wrong place.
        log.warning('m4b: could not probe every chapter duration — skipping')
        return None

    # Write to a sibling temp name and rename into place at the end.
    #
    # Audiobookshelf watches the library folder. Writing the .m4b directly to
    # its final path let the watcher scan it MID-WRITE: on 2026-07-26 it read a
    # partial file, logged "Invalid data found when processing input", and
    # marked the book invalid permanently — nothing re-checks after the write
    # completes. The file itself was perfectly good. rename(2) within a
    # filesystem is atomic, so the watcher now sees either no file or the
    # finished one, never a half of one (#38).
    tmp_out = out_path.with_name(out_path.name + '.part')
    with tempfile.TemporaryDirectory() as td:
        listing = Path(td) / 'files.txt'
        # concat demuxer: single-quote the path and escape embedded quotes.
        listing.write_text(
            '\n'.join("file '{}'".format(str(f).replace("'", r"'\''"))
                      for f in files) + '\n', encoding='utf-8')
        meta = Path(td) / 'meta.txt'
        meta.write_text(build_ffmetadata(entries, title, author, extra),
                        encoding='utf-8')

        cmd = [ffmpeg, '-v', 'error', '-y',
               '-f', 'concat', '-safe', '0', '-i', str(listing),
               '-i', str(meta)]
        if cover:
            cmd += ['-i', str(cover)]
        cmd += ['-map', '0:a', '-map_metadata', '1']
        if cover:
            cmd += ['-map', '2:v', '-c:v', 'copy',
                    '-disposition:v:0', 'attached_pic']
        cmd += ['-c:a', 'aac', '-b:a', bitrate, '-movflags', '+faststart',
                '-f', 'mp4', str(tmp_out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)

    if r.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
        log.error('m4b: ffmpeg failed (%s): %s', r.returncode, (r.stderr or '')[:400])
        for p in (tmp_out, out_path):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        return None

    # Atomic publish. os.replace overwrites silently and never leaves a gap.
    try:
        os.replace(tmp_out, out_path)
    except Exception as e:
        log.error('m4b: could not publish %s: %s', out_path, e)
        try:
            tmp_out.unlink()
        except Exception:
            pass
        return None

    total = sum(d for _, d in entries)
    log.info('m4b: wrote %s (%d chapters, %.1f min, %.1f MB)', out_path.name,
             len(entries), total / 60, out_path.stat().st_size / 1e6)
    return out_path
