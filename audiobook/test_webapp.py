import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import time
import wave
import threading
import zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from ebooklib import epub
from PIL import Image

sys.path.insert(0,str(Path(__file__).parent))
import webapp

def setup(tmp_path):
    books=tmp_path/'books';books.mkdir();(books/'Author').mkdir()
    book=epub.EpubBook();book.set_identifier('web-test');book.set_title('Web queue test');book.add_author('Test Author');book.set_language('en')
    image=io.BytesIO();Image.new('RGB',(20,30),'green').save(image,format='JPEG');book.set_cover('cover.jpg',image.getvalue())
    chapters=[]
    for n in range(2):
        c=epub.EpubHtml(title=f'Chapter {n+1}',file_name=f'{n}.xhtml',lang='en');c.content=f'<h1>Chapter {n+1}</h1><p>This is chapter {n+1} of the web queue test.</p>';book.add_item(c);chapters.append(c)
    book.toc=chapters;book.spine=chapters;book.add_item(epub.EpubNcx());book.add_item(epub.EpubNav());epub.write_epub(str(books/'Author/book.epub'),book)
    with sqlite3.connect(books/'metadata.db') as db:
        db.executescript("CREATE TABLE books (id INTEGER,title TEXT,author_sort TEXT,path TEXT,sort TEXT);CREATE TABLE data (book INTEGER,format TEXT,name TEXT);INSERT INTO books VALUES(1,'Web queue test','Test Author','Author','Web queue test');INSERT INTO data VALUES(1,'EPUB','book');")
    profiles=tmp_path/'profiles.json';profiles.write_text(json.dumps({'fast':{'url':'http://localhost:1/v1','model':'test','voice':'test','max_chars':100,'languages':['en']}}))
    q=webapp.Queue(books,tmp_path/'state',tmp_path/'library',profiles)
    return q

@pytest.fixture
def queue(tmp_path):return setup(tmp_path)

def client(queue):
    app=webapp.create_app(queue,'https://test.example','/',listen_url='https://listen.example');app.testing=True
    c=app.test_client();headers={'Remote-User':'tester'}
    token=c.get('/api/session',headers=headers,base_url='https://test.example').json['csrf']
    headers.update({'Origin':'https://test.example','X-CSRF-Token':token})
    return c,headers

def test_auth_and_csrf(queue):
    c,h=client(queue)
    assert c.get('/api/books').status_code==401
    assert c.post('/api/jobs',json={},headers={'Remote-User':'tester'},base_url='https://test.example').status_code==403
    wrong=dict(h,Origin='https://evil.example')
    assert c.post('/api/jobs',json={},headers=wrong,base_url='https://test.example').status_code==403
    response=c.get('/api/books',headers=h,base_url='https://test.example')
    assert response.json['books']==[{'id':1,'title':'Web queue test','author':'Test Author'}]
    assert 'path' not in response.json['books'][0]
    assert c.post('/api/jobs',json={'book_id':1,'profile':'fast'},headers=h,base_url='https://test.example').status_code==202

def test_durable_queue_dedup_and_pause(queue):
    before=webapp.runner.sha_file(queue.books/'Author/book.epub')
    a=queue.submit(1,'fast','auto');b=queue.submit(1,'fast','auto');assert a['id']==b['id']
    assert len(list(queue.records.glob('*.json')))==1
    queue.pause(a['id']);other=webapp.Queue(queue.books,queue.state,queue.library,queue.profiles)
    assert other.read(a['id'])['status']=='paused'
    assert other.resume(a['id'])['status']=='queued'
    assert webapp.runner.sha_file(queue.books/'Author/book.epub')==before

def test_submission_with_missing_cover_asset(queue):
    source=queue.books/'Author/book.epub'
    with zipfile.ZipFile(source) as archive:
        entries=[(item,archive.read(item)) for item in archive.infolist() if not item.filename.endswith('/cover.jpg')]
    with zipfile.ZipFile(source,'w') as archive:
        for item,data in entries: archive.writestr(item,data)
    before=webapp.runner.sha_file(source)
    c,h=client(queue)
    result=c.post('/api/jobs',json={'book_id':1,'profile':'fast','language':'auto'},headers=h,base_url='https://test.example')
    assert result.status_code==202,result.json
    assert result.json['status']=='queued'
    assert webapp.runner.sha_file(source)==before

def test_optional_cover_upload_is_persistent_and_changes_job_identity(queue):
    c,h=client(queue)
    image=io.BytesIO();Image.new('RGB',(40,50),'orange').save(image,format='PNG')
    raw=image.getvalue()
    def submit(cover):
        return c.post('/api/jobs',data={'book_id':'1','profile':'fast','language':'en','cover':(io.BytesIO(cover),'cover.png')},headers=h,base_url='https://test.example')
    base=queue.submit(1,'fast','en')
    before=webapp.runner.sha_file(queue.books/'Author/book.epub')
    response=submit(raw);assert response.status_code==202,response.json
    uploaded=response.json
    assert uploaded['id']!=base['id']
    assert submit(raw).json['id']==uploaded['id']
    assert queue.read(uploaded['id'])['cover_uploaded'] is True
    saved=queue.state/uploaded['id']/'cover-upload.jpg'
    with Image.open(saved) as cover:
        assert cover.format=='JPEG' and cover.getpixel((10,10))[0]>200
    assert webapp.runner.sha_file(queue.books/'Author/book.epub')==before
    invalid=submit(b'not an image')
    assert invalid.status_code==400 and 'Invalid cover image' in invalid.json['error']
    large=submit(b'x'*(webapp.MAX_COVER_BYTES+1))
    assert large.status_code in (400,413)

def test_missing_published_audio_requeues_same_job(queue):
    job=queue.submit(1,'fast','en')
    record=queue.read(job['id'])
    output=queue.library/'book.m4b'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'previous audiobook')
    webapp.runner.atomic_json(queue.state/job['id']/'manifest.json',{
        'status':'published','library_file':str(output),
        'library_sha256':webapp.runner.sha_file(output),
    })
    record['status']='published'
    queue.save(record)
    assert queue.submit(1,'fast','en')['status']=='published'
    output.unlink()
    refreshed=queue.submit(1,'fast','en')
    assert refreshed['id']==job['id']
    assert refreshed['status']=='queued'
    assert len(list(queue.records.glob('*.json')))==1

def test_language_and_path_validation(queue):
    with pytest.raises(ValueError):queue.submit(1,'fast','zh')
    with pytest.raises(ValueError):queue.submit('../etc/passwd','fast','en')
    with sqlite3.connect(queue.books/'metadata.db') as db:db.execute("UPDATE books SET path='../../' ")
    with pytest.raises(ValueError):queue.submit(1,'fast','en')
    with pytest.raises(ValueError):queue.resume('../etc/passwd')

def test_scheduler_restart_resumes_and_preserves_pause(queue):
    job=queue.submit(1,'fast','en');r=queue.read(job['id']);r['status']='running';queue.save(r)
    queue.loop=lambda:None
    queue.start()
    try:assert queue.read(job['id'])['status']=='queued'
    finally:queue.stop()
    queue.pause(job['id']);queue.start()
    try:assert queue.read(job['id'])['status']=='paused'
    finally:queue.stop()

def test_progress_api_hides_eta_when_paused_or_stalled(queue):
    job=queue.submit(1,'fast','en');record=queue.read(job['id']);record['status']='running'
    progress={'total_chunks':10,'completed_chunks':3,'percent':30,'phase':'synthesizing',
              'eta_seconds':70,'eta_updated_at':time.time(), 'samples':[{'chars':100,'seconds':10}]*3}
    path=queue.state/job['id']/'progress.json';webapp.runner.atomic_json(path,progress)
    assert queue.present(record)['metrics']['eta_seconds']==70
    record['status']='paused';assert queue.present(record)['metrics']['eta_seconds'] is None
    record['status']='running';progress['eta_updated_at']-=200;webapp.runner.atomic_json(path,progress)
    metrics=queue.present(record)['metrics'];assert metrics['eta_stale'] and metrics['eta_seconds'] is None
    record['status']='published';metrics=queue.present(record)['metrics']
    assert metrics['percent']==100 and metrics['phase']=='published'

def test_existing_published_jobs_get_complete_progress(queue):
    job=queue.submit(1,'fast','en');record=queue.read(job['id']);record['status']='published'
    webapp.runner.atomic_json(queue.state/job['id']/'manifest.json',{'chapters':[{'chunks':[{},{}]},{'chunks':[{}]}]})
    metrics=queue.present(record)['metrics']
    assert metrics['total_chunks']==metrics['completed_chunks']==3 and metrics['percent']==100

def test_real_worker_subprocess_pause_resume_and_packaging(queue):
    entered=threading.Event();release=threading.Event();calls=[]
    class Server(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])));calls.append(payload['input']);entered.set();release.wait(20)
            buf=io.BytesIO()
            with wave.open(buf,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b'\x10\x10'*24000)
            self.send_response(200);self.send_header('Content-Type','audio/wav');self.end_headers()
            try:self.wfile.write(buf.getvalue())
            except BrokenPipeError:pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Server);threading.Thread(target=server.serve_forever,daemon=True).start()
    config=json.loads(queue.profiles.read_text());config['fast']['url']=f'http://127.0.0.1:{server.server_port}/v1';queue.profiles.write_text(json.dumps(config))
    job=queue.submit(1,'fast','en');queue.start()
    try:
        assert entered.wait(15)
        assert queue.pause(job['id'])['status']=='paused'
        release.set();queue.resume(job['id'])
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            r=queue.read(job['id'])
            if r['status'] in ('published','failed'):break
            time.sleep(.1)
        assert r['status']=='published',(r,(queue.root/(job['id']+'.log')).read_text())
        m=webapp.runner._manifest(queue.state/job['id']);assert len(m['chapters'])==2
        assert not (queue.root/'sources'/job['id']).exists()
        count=len(calls);assert queue.submit(1,'fast','en')['status']=='published';assert len(calls)==count
    finally:release.set();queue.stop();server.shutdown()
