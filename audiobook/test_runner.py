import importlib.util, io, json, os, wave
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("runner", Path(__file__).with_name("runner.py")); runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)

def test_chunks_cover_text_and_long_cjk():
    for text in ("one two\nthree four", "漢" * 47, "x" * 31):
        got = runner.chunks(text, 10)
        assert ''.join(got) == text and all(len(x) <= 10 for x in got)
    assert runner.chunks('123456789 X', 10) == ['123456789 ', 'X']

def test_canonical_inline_and_block_text():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup('<body><p>A<i>B</i><b>C</b></p><p>漢字</p></body>', 'html.parser')
    assert runner.canonical_text(soup.body) == 'ABC\n漢字'


def test_canonical_block_following_tail_is_not_concatenated():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup('<body><div><p>A<i>B</i></p>tail<span>C</span></div>after</body>', 'html.parser')
    assert runner.canonical_text(soup.body) == 'AB\ntailC\nafter'

def test_toc_fragment_sections_keep_source_order():
    class Item:
        def get_content(self):
            return b'<html><body><h1 id="a">One</h1><p>alpha</p><h1 id="b">Two</h1><p>beta</p></body></html>'
        def get_name(self): return 'book.xhtml'
    got = list(runner._doc_sections(Item(), [('book.xhtml', 'a', 'First'), ('book.xhtml', 'b', 'Second')]))
    assert got == [('First', 'One\nalpha'), ('Second', 'Two\nbeta')]


def test_toc_fragment_keeps_block_tail_once():
    class Item:
        def get_content(self):
            return b'<body><h1 id="one">One</h1><p>A</p>tail<h1 id="two">Two</h1><div>B</div>after</body>'
        def get_name(self): return 'book.xhtml'
    got = list(runner._doc_sections(Item(), [('book.xhtml', 'one', 'One'), ('book.xhtml', 'two', 'Two')]))
    assert got == [('One', 'One\nA\ntail'), ('Two', 'Two\nB\nafter')]

def test_gutenberg_known_wrappers_removed_without_dropping_document():
    class Item:
        def get_content(self): return b'<body><div id="pg-header">boilerplate</div><p>Narrative</p><div id="pg-footer">footer</div></body>'
        def get_name(self): return 'header.xhtml'
    excluded = []
    assert list(runner._doc_sections(Item(), [], excluded)) == [('', 'Narrative')]
    assert excluded == ['pg-header', 'pg-footer']

def test_retry_429_then_success(monkeypatch):
    calls = []
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *x): pass
        def read(self): return b'wave'
    def fake(req, timeout):
        calls.append(1)
        if len(calls) == 1: raise runner.urllib.error.HTTPError('u', 429, 'x', {}, None)
        return Resp()
    monkeypatch.setattr(runner.urllib.request, 'urlopen', fake); monkeypatch.setattr(runner.time, 'sleep', lambda x: None)
    assert runner.tts('http://tts/v1', {'model':'m','voice':'v'}, 'x', 'en') == b'wave' and len(calls) == 2

def test_invalid_wav_rejected(tmp_path):
    bad = tmp_path/'bad.wav'; bad.write_bytes(b'not-a-wav')
    assert not runner.valid_wav(bad)
    good = tmp_path/'good.wav'
    with wave.open(str(good), 'wb') as w:
        w.setparams((1, 2, 16000, 4, 'NONE', 'not compressed')); w.writeframes(b'\x01\x00' * 4)
    assert runner.valid_wav(good)
    stamp = runner.wav_record(good, 'text')
    assert stamp['text_sha256'] == 'text' and stamp['audio_sha256'] == runner.sha(good.read_bytes())
    truncated = tmp_path/'truncated.wav'; truncated.write_bytes(good.read_bytes()[:-2])
    assert not runner.valid_wav(truncated)

def test_cover_is_normalized_to_jpeg(tmp_path):
    from PIL import Image
    raw = io.BytesIO(); Image.new('RGBA', (3, 3), (1, 2, 3, 255)).save(raw, 'PNG')
    out = tmp_path/'cover.jpg'; runner.write_cover(raw.getvalue(), out)
    assert out.read_bytes()[:2] == b'\xff\xd8'

def test_real_epubs_have_spine_text_and_cover():
    for name in ('/tmp/peter-rabbit.epub', '/tmp/velveteen-rabbit.epub'):
        p = Path(name)
        if not p.exists(): pytest.skip(f'{p} unavailable')
        md, chapters, cover, _ = runner.load_epub(p, p.stem)
        assert chapters and ''.join(c['text'] for c in chapters) and cover

def test_language_profile_mapping():
    got = runner.effective_profile({'voice':'af_heart', 'language_map':{'zh':'Chinese'}, 'voices_by_language':{'zh':'zf_xiaobei'}}, 'zh-CN')
    assert got['request_language'] == 'Chinese' and got['voice'] == 'zf_xiaobei'

def test_identity_includes_profile_language_and_pipeline(tmp_path):
    p = tmp_path/'b.epub'; p.write_bytes(b'x')
    cfg = {'normal': {'url':'http://x','model':'m','voice':'v','max_chars':20}}
    profiles=tmp_path/'p.json'; profiles.write_text(json.dumps(cfg))
    # Stop before EPUB parsing, but observe dedup identity path setup.
    monkeypatch = pytest.MonkeyPatch(); monkeypatch.setattr(runner, 'load_epub', lambda *p: (_ for _ in ()).throw(RuntimeError('stop')))
    with pytest.raises(RuntimeError): runner.convert(p, 'normal', profiles, tmp_path/'state', tmp_path/'lib', 'en')
    jobs = list((tmp_path/'state').iterdir()); assert len(jobs) == 1
    with pytest.raises(RuntimeError): runner.convert(p, 'normal', profiles, tmp_path/'state', tmp_path/'lib', 'fr')
    assert len(list((tmp_path/'state').iterdir())) == 2
    monkeypatch.undo()


def test_narrative_nav_filename_and_cover_page(tmp_path):
    from ebooklib import epub
    from PIL import Image
    book = epub.EpubBook(); book.set_identifier('navigation-name'); book.set_title('Test'); book.set_language('en')
    page = epub.EpubHtml(title='Cover page', file_name='cover.xhtml'); page.content = '<h1>Cover page</h1>'
    narrative = epub.EpubHtml(title='Narrative', file_name='nav.xhtml'); narrative.content = '<p>Do not skip this narrative.</p>'
    book.add_item(page); book.add_item(narrative)
    raw = io.BytesIO(); Image.new('RGB', (4,4), 'red').save(raw, 'PNG')
    book.set_cover('cover.png', raw.getvalue(), create_page=False)
    book.spine = [page, narrative]; book.add_item(epub.EpubNcx())
    path = tmp_path/'book.epub'; epub.write_epub(str(path), book)
    _, chapters, cover, _ = runner.load_epub(path)
    assert 'Do not skip this narrative.' in ''.join(c['text'] for c in chapters)
    assert cover == raw.getvalue()


def test_reject_concatenated_wav_responses(tmp_path):
    raw = io.BytesIO()
    with wave.open(raw, 'wb') as w:
        w.setparams((1, 2, 24000, 4, 'NONE', 'not compressed')); w.writeframes(b'\x01\x00' * 4)
    with pytest.raises(RuntimeError, match='concatenated WAV'):
        runner.normalize_wav(raw.getvalue() + raw.getvalue(), tmp_path/'bad.wav')


def test_pcm_transport_preserves_all_segments(tmp_path):
    raw = b'\x01\x00' * 24000 + b'\x02\x00' * 24000
    out = tmp_path/'complete.wav'
    runner.normalize_wav(raw, out, 'pcm', 24000)
    with wave.open(str(out), 'rb') as w:
        assert w.getnframes() == 48000
        assert w.readframes(48000) == raw


def test_reject_non_audio_http_response(monkeypatch):
    class Response:
        headers = {'Content-Type': 'application/json'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"error":"model unavailable"}'
    monkeypatch.setattr(runner.urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match='did not return audio'):
        runner.tts('http://tts/v1', {'model':'m','voice':'v','response_format':'pcm'}, 'Text', 'en')


def test_retry_non_audio_success_response(monkeypatch):
    responses = ['application/json', 'audio/wav']
    class Response:
        def __init__(self): self.headers = {'Content-Type': responses.pop(0)}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'audio payload'
    monkeypatch.setattr(runner.urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    monkeypatch.setattr(runner.time, 'sleep', lambda delay: None)
    assert runner.tts('http://tts/v1', {'model':'m','voice':'v'}, 'Text', 'en') == b'audio payload'
    assert responses == []


def test_long_unspaced_paragraph_after_sentence_boundary():
    text = 'A' * 70 + '.\n\n' + '汉' * 200
    parts = runner.chunks(text, 120)
    assert ''.join(parts) == text
    assert all(part.strip() and len(part) <= 120 for part in parts)
