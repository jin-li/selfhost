import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import time
import wave
import threading
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
