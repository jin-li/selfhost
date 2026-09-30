#!/usr/bin/env python3
"""Portable, restart-safe EPUB -> local OpenAI-compatible TTS runner.

The input EPUB is read only; all mutable state belongs below --state.
"""
from __future__ import annotations

import argparse, contextlib, fcntl, hashlib, io, json, os, re, shutil, subprocess
import sys, time, urllib.error, urllib.request, wave, http.client
from urllib.parse import unquote
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Comment
from ebooklib import ITEM_DOCUMENT, ITEM_COVER, ITEM_IMAGE, epub
from PIL import Image, ImageDraw, ImageFont, ImageOps

PIPELINE_VERSION = "2026-09-30.2"
BLOCKS = {"p", "div", "section", "article", "blockquote", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "tr"}


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", encoding="utf8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def safe_name(s: str) -> str:
    raw = re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE).strip("._") or "book"
    data = raw.encode("utf8")[:96]
    return data.decode("utf8", "ignore").rstrip("._") or "book"


def canonical_text(node) -> str:
    """Extract all visible text; inline tags concatenate and block tags retain boundaries."""
    parts: list[str] = []
    for kind, value in _visible_events(node):
        _append_visible(parts, kind, value)
    # Do not insert spaces between adjacent inline nodes: <i>hello</i><b>world</b>
    # is "helloworld" in the source reading order.
    text = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _visible_events(node):
    """Yield visible text once, with boundaries on both sides of block tags.

    An end boundary is necessary for XHTML such as ``<p>A</p>B``.  Keeping it
    here also makes regular chapter extraction and TOC-fragment extraction use
    exactly the same text traversal.
    """
    hidden = {"script", "style", "svg", "math", "nav"}

    def walk(el):
        if isinstance(el, Comment): return
        if isinstance(el, NavigableString):
            yield "text", str(el); return
        name = getattr(el, "name", None)
        if not name: return
        if name in hidden: return
        ident = el.get("id") if getattr(el, "attrs", None) else None
        if ident: yield "anchor", ident
        block = name in BLOCKS
        if block: yield "boundary", ""
        for child in el.children:
            yield from walk(child)
        if block: yield "boundary", ""

    yield from walk(node)


def _append_visible(parts: list[str], kind: str, value: str) -> None:
    """Coalesce adjacent structural boundaries without changing source text."""
    if kind == "boundary":
        if not parts or not parts[-1].endswith("\n"):
            parts.append("\n")
    elif kind == "text":
        parts.append(value)


def _toc_links(toc):
    if hasattr(toc, "href"): toc = [toc]
    out = []
    for x in toc or []:
        if isinstance(x, tuple):
            out.extend(_toc_links([x[0]])); out.extend(_toc_links(x[1]))
        elif hasattr(x, "href"):
            out.append((unquote(x.href.split("#", 1)[0]), unquote(x.href.partition("#")[2]), x.title or ""))
    return out


_PG_WRAPPERS = {"pg-header", "pg-footer", "pg-machine-header"}


def _doc_sections(item, toc, exclusions=None):
    soup = BeautifulSoup(item.get_content(), "html.parser")
    body = soup.body or soup
    for tag in body.find_all(id=True):
        if getattr(tag, "decomposed", False) or not getattr(tag, "attrs", None): continue
        if tag.get("id") in _PG_WRAPPERS:
            if exclusions is not None: exclusions.append(tag.get("id"))
            tag.decompose()
    href = unquote(item.get_name().split("#", 1)[0])
    wanted = {}
    for path, frag, title in toc:
        if path == href and frag and frag not in wanted and body.find(id=frag): wanted[frag] = title
    hits = [(tag.get("id"), wanted[tag.get("id")]) for tag in body.find_all(id=True) if tag.get("id") in wanted]
    if not hits:
        title = (soup.title.get_text(" ", strip=True) if soup.title else "")
        h = body.find(re.compile(r"^h[1-6]$"))
        toc_title = next((label for path, frag, label in toc if path == href and not frag and label), "")
        yield toc_title or (h.get_text(" ", strip=True) if h else "") or title, canonical_text(body)
        return
    # TOC order is often not DOM order.  Assign text while walking the DOM,
    # keeping preamble with the first real section and never moving soup nodes.
    groups = [[] for _ in hits]; current = 0
    marker = {ident: n for n, (ident, _) in enumerate(hits)}
    for kind, value in _visible_events(body):
        if kind == "anchor" and value in marker:
            current = marker[value]
        else:
            _append_visible(groups[current], kind, value)
    for (_, title), parts in zip(hits, groups):
        text = _clean_text("".join(parts))
        if text: yield title, text


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r" *\n *", "\n", text)).strip()


def load_epub(path: Path, title_fallback="") -> tuple[dict, list[dict], bytes | None, bool]:
    """Read EPUB spine/metadata/cover. Parser approach adapted from p0n1 epub_to_audiobook.

    Attribution: ebooklib + BeautifulSoup document iteration follows its
    EpubBookParser pattern, extended here to use spine order and EPUB TOC fragments.
    """
    book = epub.read_epub(str(path))
    def meta(name, default=""):
        values = book.get_metadata("DC", name)
        return values[0][0] if values else default
    md = {"title": meta("title", title_fallback or path.stem), "author": meta("creator", ""),
          "language": meta("language", "")}
    for field in ("publisher", "description", "date"):
        if meta(field): md[field] = meta(field)
    toc = _toc_links(book.toc)
    if not getattr(book, "spine", None): raise ValueError("EPUB has no readable spine")
    nav_ids = {x[0] for x in book.spine if len(x) > 1 and x[1] == "no"}
    chapters, used, exclusions = [], set(), []
    for pos, entry in enumerate(book.spine):
        ident = entry[0]
        if ident in nav_ids: continue
        item = book.get_item_with_id(ident)
        if not item or item.get_type() != ITEM_DOCUMENT: continue
        # EPUB3 navigation documents must not be spoken.
        if "nav" in (getattr(item, "properties", []) or []):
            continue
        for title, text in _doc_sections(item, toc, exclusions):
            if text:
                chapters.append({"index": len(chapters) + 1, "title": title or f"Chapter {len(chapters)+1}", "text": text})
                used.add(item.get_id())
    if not chapters: raise ValueError("EPUB has no readable spine text")
    md["excluded_wrapper_ids"] = exclusions
    cover, generated = None, False
    cover_ids = set()
    for _, attrs in book.get_metadata("OPF", "cover"):
        if isinstance(attrs, dict) and attrs.get("content"): cover_ids.add(attrs["content"])
    candidates = [item for item in book.get_items() if item.get_type() in {ITEM_COVER, ITEM_IMAGE}]
    candidates.sort(key=lambda item: not (item.get_id() in cover_ids or item.get_type() == ITEM_COVER or "cover-image" in (getattr(item, "properties", []) or [])))
    for item in candidates:
        if item.get_id() in cover_ids or item.get_type() == ITEM_COVER or "cover" in item.get_name().lower() or "cover-image" in (getattr(item, "properties", []) or []):
            try:
                raw = item.get_content()
                with Image.open(io.BytesIO(raw)) as image: image.verify()
                cover = raw; break
            except (OSError, ValueError): continue
    return md, chapters, cover, generated


def chunks(text: str, maximum: int) -> list[str]:
    """Lossless chunks, preserving canonical text exactly when rejoined."""
    if maximum < 1: raise ValueError("max_chars must be positive")
    out, rest = [], text
    while len(rest) > maximum:
        boundaries = list(re.finditer(r"[.!?。！？](?:[\"”’]?(?=\s|$)|(?=[^\x00-\x7f]))", rest[:maximum]))
        sentence_end = boundaries[-1].end() if boundaries else 0
        cut = max(rest.rfind("\n", 0, maximum), rest.rfind(" ", 0, maximum))
        if sentence_end >= maximum // 2:
            out.append(rest[:sentence_end]); rest = rest[sentence_end:]; continue
        if cut <= 0 or not rest[:cut + 1].strip():
            cut = maximum                          # long CJK; never a whitespace-only request
        else: cut += 1                              # keep separator in prior chunk
        out.append(rest[:cut]); rest = rest[cut:]
    if rest: out.append(rest)
    assert "".join(out) == text and all(len(x) <= maximum for x in out)
    return out


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, check=False, **kw)


def valid_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as w:
            if not (w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getcomptype() == "NONE" and w.getframerate() > 0 and w.getnframes() > 0): return False
            remaining, nonzero = w.getnframes(), False
            while remaining:
                raw = w.readframes(min(65536, remaining)); remaining -= len(raw) // 2
                if not raw or len(raw) % 2: return False
                nonzero |= any(raw)
            return nonzero and w.readframes(1) == b""
    except (OSError, wave.Error): return False


def normalize_wav(raw: bytes, dest: Path, audio_format="wav", sample_rate=24000) -> None:
    # Streaming endpoints can concatenate individually valid RIFF files. ffmpeg
    # may silently stop at the first one; reject such responses before decoding.
    if audio_format not in {"wav", "pcm"}: raise ValueError("Unsupported TTS transport format")
    if audio_format == "pcm" and (not raw or len(raw) % 2): raise RuntimeError("Invalid signed 16-bit PCM response")
    if audio_format == "wav" and raw[:4] == b"RIFF" and len(raw) >= 12:
        declared = int.from_bytes(raw[4:8], "little")
        if declared != 0xffffffff and declared + 8 != len(raw):
            raise RuntimeError("TTS returned an incomplete or concatenated WAV response")
    ff = shutil.which("ffmpeg")
    if not ff: raise RuntimeError("ffmpeg is required to normalize WAV")
    part = dest.with_suffix(".part")
    input_args = ["-f", "s16le", "-ar", str(sample_rate), "-ac", "1"] if audio_format == "pcm" else []
    p = run([ff, "-v", "error", "-xerror", "-y", *input_args, "-i", "pipe:0", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav", str(part)], input=raw)
    if p.returncode or not valid_wav(part):
        part.unlink(missing_ok=True); raise RuntimeError("TTS returned invalid/silent audio")
    with part.open("rb") as f: os.fsync(f.fileno())
    os.replace(part, dest); _fsync_dir(dest.parent)


def wav_record(path: Path, text_hash: str) -> dict:
    return {"text_sha256": text_hash, "audio_sha256": sha(path.read_bytes())}


def normalize_cover(raw: bytes) -> bytes:
    """Decode untrusted artwork, limit its dimensions, and discard image metadata."""
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width < 1 or source.height < 1 or source.width * source.height > 25_000_000:
                raise ValueError("Cover dimensions must be at most 25 megapixels")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, "JPEG", quality=90)
            return output.getvalue()
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid cover image: {error}") from error


def save_cover_jpeg(raw: bytes, dest: Path) -> None:
    """Commit a validated JPEG without altering its content hash."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with part.open("wb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    os.replace(part, dest); _fsync_dir(dest.parent)


def write_cover(raw: bytes, dest: Path) -> None:
    """Normalize an EPUB or uploaded image to an attachable JPEG."""
    save_cover_jpeg(normalize_cover(raw), dest)


def write_title_cover(title: str, author: str, dest: Path) -> None:
    """Give a coverless book readable artwork without requiring an upload."""
    image = Image.new("RGB", (800, 800), "#142b3c")
    draw = ImageDraw.Draw(image)
    font_path = next((path for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ) if Path(path).is_file()), None)
    def font(size):
        return ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default(size=size)
    def lines_for(value, face):
        lines, line = [], ""
        for char in (value or "Untitled"):
            if char == "\n":
                lines.append(line.strip()); line = ""; continue
            candidate = line + char
            if line and draw.textlength(candidate, font=face) > 640:
                lines.append(line.strip()); line = char.lstrip()
            else:
                line = candidate
        if line.strip(): lines.append(line.strip())
        return lines or ["Untitled"]
    for size in range(72, 17, -2):
        face = font(size)
        lines = lines_for(title, face)
        step = int(size * 1.35)
        if len(lines) * step <= 450: break
    draw.text((80, 95), "AUDIOBOOK", font=font(23), fill="#81b9c6")
    top = 355 - (len(lines) * step) // 2
    for line in lines:
        draw.text((80, top), line, font=face, fill="white")
        top += step
    if author:
        author_face = font(25)
        author_lines = lines_for(author, author_face)[:2]
        for offset, line in enumerate(author_lines):
            draw.text((80, 675 + offset * 35), line, font=author_face, fill="#c7dce3")
    output = io.BytesIO(); image.save(output, "JPEG", quality=90)
    save_cover_jpeg(output.getvalue(), dest)


def language_for_epub(path: Path) -> str:
    values = epub.read_epub(str(path)).get_metadata("DC", "language")
    return (values[0][0] if values else "en") or "en"


def effective_profile(config: dict, language: str) -> dict:
    cfg = json.loads(json.dumps(config))
    key = language.lower().replace("_", "-").split("-", 1)[0]
    key = {"eng":"en", "zho":"zh", "chi":"zh", "deu":"de", "ger":"de", "fra":"fr", "fre":"fr", "jpn":"ja", "kor":"ko", "spa":"es", "ita":"it", "por":"pt", "rus":"ru"}.get(key, key)
    if config.get("languages") and key not in config["languages"]:
        raise ValueError(f"Profile does not support language {language}; choose another profile")
    cfg["request_language"] = cfg.get("language_map", {}).get(language, cfg.get("language_map", {}).get(key, key))
    cfg["voice"] = cfg.get("voices_by_language", {}).get(language, cfg.get("voices_by_language", {}).get(key, cfg["voice"]))
    return cfg


def tts(url: str, config: dict, text: str, language: str, attempts: int = 3) -> bytes:
    payload = json.dumps({"model": config["model"], "voice": config["voice"], "input": text,
                          "response_format": config.get("response_format", "wav"), "stream": config.get("stream", False), "speed": config.get("speed", 1), "language": config.get("request_language", language)}).encode()
    retryable = {429, 500, 502, 503, 504}
    for n in range(attempts):
        try:
            req = urllib.request.Request(url.rstrip("/") + "/audio/speech", payload, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=float(config.get("timeout", 120))) as r:
                content_type = getattr(r, "headers", {}).get("Content-Type", "").split(";", 1)[0].lower()
                if content_type and not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
                    raise RuntimeError("TTS endpoint did not return audio: " + content_type)
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in retryable or n + 1 == attempts: raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead, RuntimeError):
            if n + 1 == attempts: raise
        time.sleep(min(4, 0.5 * (2 ** n)))
    raise RuntimeError("unreachable")


def _manifest(job: Path) -> dict:
    p = job / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def cleanup_transient(job: Path) -> None:
    shutil.rmtree(job / "chunks", ignore_errors=True); shutil.rmtree(job / "audio", ignore_errors=True)
    (job / "book.m4b").unlink(missing_ok=True)


def job_identity(source_hash: str, config: dict, language: str, cover_hash: str = "") -> str:
    payload = {"source": source_hash, "profile": config, "version": PIPELINE_VERSION, "language": language}
    if cover_hash: payload["cover_sha256"] = cover_hash
    return sha(json.dumps(payload, sort_keys=True))


def convert(source: Path, profile_name: str, profiles: Path, state: Path, library: Path, language="en", profile_override=None, cover_override: Path | None = None) -> str:
    cfgs = json.loads(profiles.read_text()) if profile_override is None else {}
    if language == "auto": language = language_for_epub(source)
    config = effective_profile(profile_override if profile_override is not None else (cfgs[profile_name] if profile_name in cfgs else (_ for _ in ()).throw(ValueError("unknown profile"))), language)
    source_hash = sha_file(source)
    cover_hash = sha_file(cover_override) if cover_override is not None else ""
    identity = job_identity(source_hash, config, language, cover_hash)
    job = state / identity; job.mkdir(parents=True, exist_ok=True)
    with (job / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = _manifest(job)
        if manifest.get("status") == "published":
            dest = Path(manifest.get("library_file", ""))
            if dest.exists() and manifest.get("library_sha256") == sha_file(dest):
                cleanup_transient(job); return identity
        snap = job / "source.epub"
        if not snap.exists():
            part = snap.with_suffix(".part")
            with source.open("rb") as src, part.open("wb") as dst:
                shutil.copyfileobj(src, dst); dst.flush(); os.fsync(dst.fileno())
            os.replace(part, snap); _fsync_dir(job); os.chmod(snap, 0o444)
            if sha_file(snap) != source_hash: raise RuntimeError("source changed during snapshot")
        elif sha_file(snap) != source_hash: raise RuntimeError("immutable source snapshot mismatch")
        # A resumed snapshot is named source.epub; retain the original fallback
        # title recorded at first checkpoint rather than silently changing it.
        fallback_title = (manifest.get("metadata") or {}).get("title") or source.stem
        md, chapters, cover, generated = load_epub(snap, fallback_title)
        cover_path = job / "cover.jpg"
        if cover_override is not None:
            write_cover(cover_override.read_bytes(), cover_path)
            cover_source = "uploaded"
        elif cover:
            write_cover(cover, cover_path)
            cover_source = "epub"
        else:
            write_title_cover(md["title"], md["author"], cover_path)
            cover_source = "title"
        generated = cover_source == "title"
        manifest = {"version": PIPELINE_VERSION, "identity": identity, "source_sha256": source_hash,
                    "profile_name": profile_name, "profile": config, "language": language, "metadata": md,
                    "cover_generated": generated, "cover_source": cover_source,
                    "status": "rendering", "chapters": []}
        atomic_json(job / "manifest.json", manifest)
        print(f"job {identity}", file=sys.stderr, flush=True)
        for chapter in chapters:
            rec = {"index": chapter["index"], "title": chapter["title"], "text": chapter["text"],
                   "sha256": sha(chapter["text"]), "chunks": [], "state": "pending"}
            manifest["chapters"].append(rec)
            cdir = job / "chunks" / f"{chapter['index']:04d}"; cdir.mkdir(parents=True, exist_ok=True)
            passages = chunks(chapter["text"], int(config["max_chars"]))
            for n, text in enumerate(passages, 1):
                print(f"job {identity} chapter {chapter['index']}/{len(chapters)} chunk {n}/{len(passages)}", file=sys.stderr, flush=True)
                wav = cdir / f"{n:05d}.wav"; h = sha(text)
                cres = {"index": n, "chapter_index": chapter["index"], "chapter_title": chapter["title"],
                        "text": text, "sha256": h, "wav": str(wav.relative_to(job)), "state": "pending"}
                stamp = wav.with_suffix(".json")
                try: cached = json.loads(stamp.read_text()) if stamp.exists() else {}
                except (OSError, ValueError): cached = {}
                if wav.exists() and valid_wav(wav) and cached == wav_record(wav, h):
                    cres.update(cached); cres["state"] = "done"
                else:
                    wav.unlink(missing_ok=True); stamp.unlink(missing_ok=True)
                    try:
                        for audio_attempt in range(3):
                            with (state / (".tts-" + sha(config["url"].rstrip("/")) + ".lock")).open("w") as backend_lock:
                                fcntl.flock(backend_lock, fcntl.LOCK_EX)
                                raw = tts(config["url"], config, text, language)
                            try:
                                normalize_wav(raw, wav, config.get("response_format", "wav"), int(config.get("pcm_sample_rate", 24000)))
                                break
                            except RuntimeError:
                                if audio_attempt == 2: raise
                                time.sleep(2 ** audio_attempt)
                    except Exception as e:
                        manifest["status"] = "failed"; manifest["error"] = str(e); atomic_json(job / "manifest.json", manifest)
                        raise
                    cached = wav_record(wav, h); atomic_json(stamp, cached)
                    cres.update(cached); cres["state"] = "done"
                rec["chunks"].append(cres)
                # Per-chunk stamp is the durable checkpoint; avoid rewriting the
                # growing full-book manifest for every short TTS request.
            assert "".join(x["text"] for x in rec["chunks"]) == chapter["text"]
            rec["state"] = "done"; atomic_json(job / "manifest.json", manifest)
        try:
            # Stream concatenate each chapter. Never assemble PCM in Python memory.
            audio = job / "audio"; audio.mkdir(exist_ok=True)
            ff = shutil.which("ffmpeg")
            if not ff: raise RuntimeError("ffmpeg required")
            for rec in manifest["chapters"]:
                out = audio / f"{rec['index']:04d}_{safe_name(rec['title'])}.wav"
                out.unlink(missing_ok=True)  # rebuild chapter from validated chunks every run
                listing = audio / f"{rec['index']:04d}.txt"
                listing.write_text("".join("file '%s'\n" % (job / x["wav"]).as_posix().replace("'", r"'\\''") for x in rec["chunks"]))
                p = run([ff, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out.with_suffix(".part.wav"))])
                if p.returncode or not valid_wav(out.with_suffix(".part.wav")): raise RuntimeError("chapter concat failed")
                os.replace(out.with_suffix(".part.wav"), out)
            sys.path.insert(0, str(Path(__file__).parent)); import m4b
            old_files, old_title = m4b.chapter_files, m4b.chapter_title
            titles = {f"{c['index']:04d}_{safe_name(c['title'])}.wav": c['title'] for c in manifest['chapters']}
            m4b.chapter_files = lambda d: [Path(d) / name for name in titles]
            m4b.chapter_title = lambda p: titles[p.name]  # retain punctuation lost in safe filenames
            try: result = m4b.build_m4b(audio, job / "book.m4b", md["title"], md["author"], cover=cover_path,
                                        extra={k: md[k] for k in ("publisher", "description", "date", "language") if md.get(k)})
            finally: m4b.chapter_files, m4b.chapter_title = old_files, old_title
            if not result: raise RuntimeError("M4B packaging failed")
            check = run(["ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-show_format", "-of", "json", str(result)], text=True)
            info = json.loads(check.stdout) if not check.returncode else {}
            got_titles = [c.get("tags", {}).get("title", "") for c in info.get("chapters", [])]
            want_titles = [c["title"] for c in manifest["chapters"]]
            tags = info.get("format", {}).get("tags", {})
            has_cover = any(s.get("disposition", {}).get("attached_pic") for s in info.get("streams", []))
            ends = [float(c.get("end_time", 0)) for c in info.get("chapters", [])]
            starts = [float(c.get("start_time", 0)) for c in info.get("chapters", [])]
            duration = float(info.get("format", {}).get("duration", 0))
            if (len(got_titles) != len(chapters) or got_titles != want_titles or
                    tags.get("title", "") != md["title"] or tags.get("artist", "") != md["author"] or not has_cover or
                    any(b < a for a, b in zip(starts, starts[1:])) or not ends or abs(ends[-1] - duration) > 0.25):
                raise RuntimeError("M4B metadata/chapter/cover verification failed")
            if run([ff, "-v", "error", "-xerror", "-i", str(result), "-f", "null", "-"], text=True).returncode: raise RuntimeError("M4B decode failed")
            book_dir = library / safe_name(md["author"] or "Unknown") / f"{safe_name(md['title'])} [{safe_name(profile_name)}-{identity[:12]}]"
            book_dir.mkdir(parents=True, exist_ok=True); dest = book_dir / f"{safe_name(md['title'])}.m4b"; part = dest.with_suffix(".part")
            with result.open("rb") as src, part.open("wb") as dst:
                shutil.copyfileobj(src, dst); dst.flush(); os.fsync(dst.fileno())
            os.replace(part, dest); _fsync_dir(book_dir)
            manifest["status"] = "published"; manifest["library_file"] = str(dest); manifest["library_sha256"] = sha_file(dest); atomic_json(job / "manifest.json", manifest)
            cleanup_transient(job)
        except Exception as error:
            manifest["status"] = "failed"
            manifest["error"] = str(error)
            atomic_json(job / "manifest.json", manifest)
            raise
    return identity


def main():
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert"); c.add_argument("path", type=Path); c.add_argument("--profile", default="normal"); c.add_argument("--language", default="auto"); c.add_argument("--profiles", type=Path, default=Path("/config/profiles.json")); c.add_argument("--state", type=Path, default=Path("/state")); c.add_argument("--library", type=Path, default=Path("/library"))
    s = sub.add_parser("status"); s.add_argument("job"); s.add_argument("--state", type=Path, default=Path("/state"))
    r = sub.add_parser("resume"); r.add_argument("job"); r.add_argument("--profiles", type=Path, default=Path("/config/profiles.json")); r.add_argument("--state", type=Path, default=Path("/state")); r.add_argument("--library", type=Path, default=Path("/library"))
    a = p.parse_args()
    if a.cmd == "convert": print(convert(a.path, a.profile, a.profiles, a.state, a.library, a.language))
    elif a.cmd == "status": print((a.state / a.job / "manifest.json").read_text())
    else:
        m = _manifest(a.state / a.job)
        if m.get("version") != PIPELINE_VERSION: raise RuntimeError("pipeline version mismatch; resume refused")
        uploaded = a.state / a.job / "cover-upload.jpg"
        if m.get("cover_source") == "uploaded" and not uploaded.is_file():
            raise RuntimeError("uploaded cover is missing; restore the job state before resuming")
        print(convert(a.state / a.job / "source.epub", m['profile_name'], a.profiles, a.state, a.library,
                      m['language'], m['profile'], uploaded if m.get("cover_source") == "uploaded" else None))
if __name__ == "__main__": main()
