"""Exercise real EPUB parsing and ffmpeg packaging with a deterministic TTS fixture."""
import importlib.util
import io
import json
import math
from pathlib import Path
import struct
import subprocess
import wave
import zipfile

from ebooklib import epub
from PIL import Image
import pytest

spec = importlib.util.spec_from_file_location('runner', Path(__file__).with_name('runner.py'))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def book(path, with_cover=True):
    b = epub.EpubBook()
    b.set_identifier('integration-book')
    b.set_title('Two chapters: A & B')
    b.add_author('Test Author')
    b.set_language('en')
    img = io.BytesIO()
    Image.new('RGB', (120,160), (40,90,120)).save(img, format='JPEG')
    if with_cover: b.set_cover('cover.jpg', img.getvalue())
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


def remove_asset(path, suffix):
    with zipfile.ZipFile(path) as archive:
        entries=[(item,archive.read(item)) for item in archive.infolist() if not item.filename.endswith(suffix)]
    with zipfile.ZipFile(path,'w') as archive:
        for item,data in entries: archive.writestr(item,data)


@pytest.mark.parametrize('missing_manifest_image', [False, True])
def test_coverless_book_gets_title_art_and_valid_m4b(tmp_path, monkeypatch, missing_manifest_image):
    source=tmp_path/'coverless.epub'; book(source, with_cover=missing_manifest_image)
    if missing_manifest_image: remove_asset(source, '/cover.jpg')
    original=r.sha_file(source)
    assert r.language_for_epub(source)=='en'
    md, chapters, cover, _=r.load_epub(source)
    assert md['title']=='Two chapters: A & B' and len(chapters)==2 and cover is None
    config=tmp_path/'profiles.json'
    config.write_text(json.dumps({'fast': {'url':'http://local/v1','model':'test','voice':'test','max_chars':100}}))
    monkeypatch.setattr(r,'tts',lambda *args: pcm())
    state=tmp_path/'jobs'; library=tmp_path/'library'
    identity=r.convert(source,'fast',config,state,library,'en')
    manifest=json.loads((state/identity/'manifest.json').read_text())
    assert manifest['status']=='published' and manifest['cover_source']=='title'
    assert len(manifest['chapters'])==2
    with Image.open(state/identity/'cover.jpg') as image:
        assert image.format=='JPEG' and image.size==(800,800)
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_chapters','-of','json',manifest['library_file']]))
    assert len(probe['chapters'])==2
    assert any(stream.get('disposition',{}).get('attached_pic') for stream in probe['streams'])
    assert r.sha_file(source)==original
    if missing_manifest_image:
        assert manifest['metadata']['missing_image_assets']==['EPUB/cover.jpg']


def test_missing_chapter_is_never_silently_skipped(tmp_path):
    source=tmp_path/'incomplete.epub'; book(source)
    remove_asset(source, '/first.xhtml')
    for reader in (r.load_epub,r.language_for_epub):
        with pytest.raises(ValueError,match='missing required file.*first.xhtml'):
            reader(source)


def test_uploaded_cover_overrides_embedded_art(tmp_path, monkeypatch):
    source=tmp_path/'book.epub'; book(source)
    config=tmp_path/'profiles.json'
    config.write_text(json.dumps({'fast': {'url':'http://local/v1','model':'test','voice':'test','max_chars':100}}))
    uploaded=tmp_path/'uploaded.png'; Image.new('RGB',(80,100),'orange').save(uploaded)
    monkeypatch.setattr(r,'tts',lambda *args: pcm())
    state=tmp_path/'jobs'
    identity=r.convert(source,'fast',config,state,tmp_path/'library','en',cover_override=uploaded)
    manifest=json.loads((state/identity/'manifest.json').read_text())
    assert manifest['status']=='published' and manifest['cover_source']=='uploaded'
    with Image.open(state/identity/'cover.jpg') as image:
        assert image.getpixel((20,20))[0]>200
    assert r.sha_file(source)==manifest['source_sha256']


def test_progress_counts_resume_timing_and_packaging(tmp_path, monkeypatch):
    source=tmp_path/'book.epub';book(source)
    profiles=tmp_path/'profiles.json'
    profiles.write_text(json.dumps({'fast':{'url':'http://local/v1','model':'test','voice':'test','max_chars':10}}))
    state=tmp_path/'jobs';library=tmp_path/'library';clock=[100.0];calls=[]
    monkeypatch.setattr(r.time,'monotonic',lambda:clock[0])
    def synthesize(*args):
        calls.append(args[2]);clock[0]+=10
        if len(calls)==4:raise RuntimeError('interrupted')
        return pcm()
    monkeypatch.setattr(r,'tts',synthesize)
    with pytest.raises(RuntimeError,match='interrupted'):r.convert(source,'fast',profiles,state,library,'en')
    job=next(path for path in state.iterdir() if (path/'manifest.json').exists())
    progress=json.loads((job/'progress.json').read_text())
    assert progress['completed_chunks']==3 and progress['total_chunks']>3
    assert progress['eta_seconds']>0 and progress['percent']<100
    assert [sample['seconds'] for sample in progress['samples']]==[10,10,10]
    clock[0]+=86400  # A day paused must not become a synthesis timing sample.
    _,chapters,_,_=r.load_epub(source)
    pending=[text for chapter in chapters for text in r.chunks(chapter['text'],10)][3:]
    def resumed(*args):
        assert args[2]==pending.pop(0)
        assert json.loads((job/'progress.json').read_text())['completed_chunks']>=3
        clock[0]+=10;return pcm()
    monkeypatch.setattr(r,'tts',resumed)
    import m4b
    package=m4b.build_m4b
    def checked_package(*args,**kwargs):
        current=json.loads((job/'progress.json').read_text())
        assert current['phase']=='packaging' and current['percent']==99 and current['eta_seconds'] is None
        return package(*args,**kwargs)
    monkeypatch.setattr(m4b,'build_m4b',checked_package)
    assert r.convert(source,'fast',profiles,state,library,'en')==job.name
    final=json.loads((job/'progress.json').read_text())
    assert final['completed_chunks']==final['total_chunks'] and final['percent']==100
    assert final['phase']=='published' and all(sample['seconds']==10 for sample in final['samples'])
    assert not pending
