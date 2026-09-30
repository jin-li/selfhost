"""Exercise real EPUB parsing and ffmpeg packaging with a deterministic TTS fixture."""
import importlib.util
import io
import json
import math
from pathlib import Path
import struct
import wave

from ebooklib import epub
from PIL import Image
import pytest

spec = importlib.util.spec_from_file_location('runner', Path(__file__).with_name('runner.py'))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def book(path):
    b = epub.EpubBook()
    b.set_identifier('integration-book')
    b.set_title('Two chapters: A & B')
    b.add_author('Test Author')
    b.set_language('en')
    img = io.BytesIO()
    Image.new('RGB', (120,160), (40,90,120)).save(img, format='JPEG')
    b.set_cover('cover.jpg', img.getvalue())
    first = epub.EpubHtml(title='First', file_name='first.xhtml', lang='en')
    first.content = '<h1>First</h1><p>Short chapter one.</p>'
    second = epub.EpubHtml(title='Second', file_name='second.xhtml', lang='en')
    second.content = '<h1>Second</h1><p>Short chapter two.</p>'
    b.add_item(second)
    b.add_item(first)  # manifest order intentionally differs from reading order
    b.toc = (epub.Link('first.xhtml','First','a'), epub.Link('second.xhtml','Second','b'))
    b.add_item(epub.EpubNcx()); b.add_item(epub.EpubNav())
    b.spine = ['nav', first, second]
    epub.write_epub(str(path),b)


def pcm():
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(2*math.pi*440*n/24000))) for n in range(24000)))
    return buf.getvalue()


def test_interrupted_resume_packaging_and_dedup(tmp_path, monkeypatch):
    source=tmp_path/'book.epub'; book(source)
    original=r.sha_file(source)
    config=tmp_path/'profiles.json'
    config.write_text(json.dumps({'fast': {'url':'http://local/v1','model':'test','voice':'test','max_chars':100}}))
    state=tmp_path/'jobs'; library=tmp_path/'library'
    calls=[]
    def first_run(url,cfg,text,language):
        calls.append(text)
        if len(calls)==2: raise RuntimeError('simulated interruption')
        return pcm()
    monkeypatch.setattr(r,'tts',first_run)
    with pytest.raises(RuntimeError,match='simulated interruption'):
        r.convert(source,'fast',config,state,library,'en')
    job=next(path for path in state.iterdir() if (path/'manifest.json').is_file())
    first_wave=next((job/'chunks'/'0001').glob('*.wav'))
    wave_sha=r.sha_file(first_wave)
    def resumed(url,cfg,text,language):
        calls.append(text)
        assert 'chapter one' not in text, 'completed first chunk was synthesized twice'
        return pcm()
    monkeypatch.setattr(r,'tts',resumed)
    identity=r.convert(source,'fast',config,state,library,'en')
    manifest=json.loads((job/'manifest.json').read_text())
    assert manifest['status']=='published'
    assert manifest['chapters'][0]['chunks'][0]['audio_sha256']==wave_sha
    assert len(manifest['chapters'])==2
    assert not (job/'chunks').exists() and not (job/'audio').exists()
    assert Path(manifest['library_file']).is_file()
    assert Path(manifest['library_file']).parent != library
    assert r.sha_file(source)==original
    count=len(calls)
    assert r.convert(source,'fast',config,state,library,'en')==identity
    assert len(calls)==count
