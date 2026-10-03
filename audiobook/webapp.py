"""Authenticated web queue around the existing durable conversion worker."""
import base64
import io
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, request, session, send_from_directory
from werkzeug.exceptions import HTTPException
from PIL import Image
import runner

JOB_ID = re.compile(r'^[0-9a-f]{64}$')
MAX_COVER_BYTES = 5 * 1024 * 1024

class Queue:
    def __init__(self, books, state, library, profiles):
        self.books, self.state, self.library, self.profiles = map(Path, (books,state,library,profiles))
        self.root = self.state / '.web'
        self.records = self.root / 'queue'
        self.records.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        self.stop_event = threading.Event()
        self.active = None
        self.thread = None
        self.process = None
        self.completed_metrics = {}

    def read(self, identity):
        if not JOB_ID.fullmatch(identity): raise ValueError('Invalid job ID')
        path = self.records / (identity + '.json')
        if not path.exists(): raise ValueError('Unknown web job')
        return json.loads(path.read_text())

    def save(self, record):
        runner.atomic_json(self.records / (record['id'] + '.json'), record)

    def catalog(self, query='', book_id=None):
        with sqlite3.connect((self.books / 'metadata.db').as_uri()+'?mode=ro', uri=True, timeout=10) as db:
            db.row_factory = sqlite3.Row
            sql = "SELECT b.id,b.title,b.author_sort AS author,b.path,d.name FROM books b JOIN data d ON d.book=b.id WHERE d.format='EPUB'"
            args=[]
            if book_id is not None: sql+=' AND b.id=?';args=[int(book_id)]
            elif query: sql+=' AND (b.title LIKE ? OR b.author_sort LIKE ?)';args=['%'+query[:200]+'%']*2
            total=db.execute('SELECT count(*) FROM ('+sql+')',args).fetchone()[0]
            rows=[dict(x) for x in db.execute(sql+' ORDER BY b.sort LIMIT 100',args)]
        return rows,total

    def source_for_book(self, book_id):
        rows,_=self.catalog(book_id=book_id)
        if not rows: raise ValueError('Book has no EPUB edition')
        book=rows[0]
        source=(self.books/book['path']/(book['name']+'.epub')).resolve()
        if not source.is_relative_to(self.books.resolve()) or not source.is_file(): raise ValueError('EPUB is unavailable')
        return book,source

    def cover_preview(self, book_id):
        _,source=self.source_for_book(book_id)
        raw=runner.extract_cover(runner.read_epub(source))
        if raw is None: return None
        with Image.open(io.BytesIO(runner.normalize_cover(raw))) as image:
            image.thumbnail((320,480), Image.Resampling.LANCZOS)
            output=io.BytesIO();image.save(output,"JPEG",quality=85)
        return "data:image/jpeg;base64,"+base64.b64encode(output.getvalue()).decode("ascii")

    def submit(self, book_id, profile, language, cover=None):
        profiles=json.loads(self.profiles.read_text())
        if profile not in profiles: raise ValueError('Unknown profile')
        book,source=self.source_for_book(book_id)
        if language=='auto': language=runner.language_for_epub(source)
        if not isinstance(language,str) or not re.fullmatch(r'[A-Za-z_-]{2,16}',language): raise ValueError('Invalid language')
        settings=runner.effective_profile(profiles[profile],language)
        source_hash=runner.sha_file(source)
        normalized_cover=runner.normalize_cover(cover) if cover is not None else None
        cover_hash=runner.sha(normalized_cover) if normalized_cover is not None else ''
        identity=runner.job_identity(source_hash,settings,language,cover_hash)
        with self.guard:
            if normalized_cover is not None:
                runner.save_cover_jpeg(normalized_cover,self.state/identity/'cover-upload.jpg')
            path=self.records/(identity+'.json')
            if path.exists():
                record=self.read(identity)
                if record['status']=='published':
                    manifest=runner._manifest(self.state/identity)
                    dest=Path(manifest.get('library_file',''))
                    if not (manifest.get('status')=='published' and dest.is_file() and runner.sha_file(dest)==manifest.get('library_sha256')):
                        record.update(status='queued',error='')
                        self.save(record)
                return self.present(record)
            folder=self.root/'sources'/identity;folder.mkdir(parents=True,exist_ok=True)
            snap=folder/source.name
            with source.open('rb') as src, snap.with_suffix('.part').open('wb') as dst:
                shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
            os.replace(snap.with_suffix('.part'),snap);runner._fsync_dir(folder)
            if runner.sha_file(snap)!=source_hash: raise ValueError('Ebook changed; select it again')
            os.chmod(snap,0o444)
            record={'id':identity,'title':book['title'],'profile':profile,'settings':settings,'source':str(snap),'language':language,'status':'queued','created':time.time(),'error':'','version':runner.PIPELINE_VERSION,'cover_uploaded':normalized_cover is not None}
            manifest=runner._manifest(self.state/identity)
            if manifest.get('status')=='published':
                dest=Path(manifest.get('library_file',''))
                if dest.is_file() and runner.sha_file(dest)==manifest.get('library_sha256'):record['status']='published'
            self.save(record)
            if record['status']=='published':shutil.rmtree(folder)
            return self.present(record)

    def present(self, record):
        data={key:record.get(key,'') for key in ('id','title','profile','status','created','error')}
        logfile=self.root/(record['id']+'.log')
        data['progress']=''
        if logfile.exists():
            with logfile.open('rb') as f:
                f.seek(max(0,logfile.stat().st_size-4096));tail=f.read().decode('utf8','replace')
            matches=re.findall(r'chapter (\d+/\d+) chunk (\d+/\d+)',tail)
            if matches:data['progress']='Chapter '+matches[-1][0]+' · chunk '+matches[-1][1]
        if data['status']=='published': data['progress']='Ready in the listening library'
        data['metrics']=self.metrics(record)
        return data

    def metrics(self, record):
        job=self.state/record['id']
        try: progress=json.loads((job/'progress.json').read_text())
        except (OSError,ValueError):progress={}
        if record['status']=='published' and not progress:
            if record['id'] not in self.completed_metrics:
                manifest=runner._manifest(job)
                total=sum(len(chapter.get('chunks',[])) for chapter in manifest.get('chapters',[]))
                self.completed_metrics[record['id']]={'total_chunks':total,'completed_chunks':total}
            progress=self.completed_metrics[record['id']]
        data={key:progress.get(key) for key in ('total_chunks','completed_chunks','percent','phase','eta_seconds','eta_updated_at')}
        data['eta_stale']=False
        if record['status']=='published':data.update(percent=100,phase='published',eta_seconds=None)
        elif record['status']!='running' or progress.get('phase')!='synthesizing':data['eta_seconds']=None
        elif progress.get('eta_updated_at'):
            samples=progress.get('samples',[])
            average=sum(sample['seconds'] for sample in samples)/len(samples) if samples else 0
            if time.time()-progress['eta_updated_at']>max(120,3*average):
                data.update(eta_seconds=None,eta_stale=True)
        return data

    def jobs(self):
        with self.guard:
            records=[json.loads(p.read_text()) for p in self.records.glob('*.json')]
            # Expose earlier CLI jobs too, without importing them into the queue.
            seen={r['id'] for r in records}
            for path in self.state.glob('*/manifest.json'):
                m=json.loads(path.read_text());identity=path.parent.name
                if m.get('status')=='rejected':continue
                if identity not in seen and JOB_ID.fullmatch(identity):
                    records.append({'id':identity,'title':m.get('metadata',{}).get('title',identity[:12]),'profile':m.get('profile_name',''),'status':'published' if m.get('status')=='published' else 'failed','error':m.get('error',''),'created':path.stat().st_mtime})
            return [self.present(r) for r in sorted(records,key=lambda r:r['created'],reverse=True)]

    def resume(self, identity):
        with self.guard:
            if not JOB_ID.fullmatch(identity):raise ValueError('Invalid job ID')
            if not (self.records/(identity+'.json')).exists():
                m=runner._manifest(self.state/identity)
                if not m:raise ValueError('Unknown job')
                record={'id':identity,'title':m['metadata']['title'],'profile':m['profile_name'],'settings':m['profile'],'language':m['language'],'source':str(self.state/identity/'source.epub'),'created':time.time(),'status':'failed','version':m['version']}
            else: record=self.read(identity)
            if record['version']!=runner.PIPELINE_VERSION:raise ValueError('Worker version changed; start a new conversion')
            if record['status'] in ('queued','running','published'):return self.present(record)
            record.update(status='queued',error='');self.save(record);return self.present(record)

    def pause(self, identity):
        with self.guard:
            record=self.read(identity)
            if record['status'] not in ('queued','running'):return self.present(record)
            record['status']='paused';self.save(record)
            if self.active==identity and self.process:
                self.terminate(self.process)
            return self.present(record)

    @staticmethod
    def terminate(process):
        if process.poll() is not None:return
        with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
            process.wait()

    def start(self):
        self.lock=(self.root/'scheduler.lock').open('w')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with self.guard:
            for p in self.records.glob('*.json'):
                r=json.loads(p.read_text())
                if r['status']=='running':r['status']='queued';self.save(r)
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def loop(self):
        while not self.stop_event.is_set():
            with self.guard:
                pending=sorted((json.loads(p.read_text()) for p in self.records.glob('*.json')),key=lambda r:r['created'])
                record=next((r for r in pending if r['status']=='queued'),None)
                if record:
                    identity=record['id'];record['status']='running';record['error']='';self.save(record)
                    log=(self.root/(identity+'.log')).open('w')
                    try:
                        self.process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'execute',str(self.records/(identity+'.json')),str(self.state),str(self.library)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                        self.active=identity;process=self.process
                    except Exception as e:
                        record.update(status='failed',error=str(e));self.save(record);log.close();continue
            if not record:self.stop_event.wait(0.5);continue
            result=process.wait();log.close()
            with self.guard:
                record=self.read(identity)
                if record['status']=='running':
                    manifest=runner._manifest(self.state/identity)
                    if result==0 and manifest.get('status')=='published':
                        record.update(status='published',error='');shutil.rmtree(self.root/'sources'/identity,ignore_errors=True)
                    elif self.stop_event.is_set():record['status']='queued'
                    else:record.update(status='failed',error=manifest.get('error') or 'Conversion stopped; resume to retry. See server logs for details.')
                    self.save(record)
                self.active=None;self.process=None

    def stop(self):
        self.stop_event.set()
        with self.guard:
            if self.process:self.terminate(self.process)
        if self.thread:self.thread.join(timeout=15)
        if hasattr(self,'lock'):self.lock.close()


def create_app(queue, origin, base_path='/', require_auth=True, listen_url=''):
    if not origin.startswith(('https://','http://')):raise ValueError('Set WEB_ORIGIN to the external origin')
    app=Flask(__name__,static_folder=None)
    key=queue.root/'session-key'
    try:
        fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_hex(32))
    except FileExistsError:pass
    app.config.update(SECRET_KEY=key.read_text(),MAX_CONTENT_LENGTH=MAX_COVER_BYTES+65536,SESSION_COOKIE_NAME='audiobook_queue',SESSION_COOKIE_PATH=base_path,SESSION_COOKIE_SECURE=origin.startswith('https:'),SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict')
    @app.before_request
    def protect():
        if request.path=='/health':return
        if require_auth and not request.headers.get('Remote-User'):return jsonify(error='Authentication required'),401
        if request.method=='POST':
            if request.headers.get('Origin')!=origin or not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),session.get('csrf','missing')):
                return jsonify(error='Session expired; refresh the page and try again'),403
            if not request.is_json and not (request.path=='/api/jobs' and request.mimetype=='multipart/form-data'):
                return jsonify(error='JSON or multipart request required'),415
    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response
    @app.errorhandler(ValueError)
    @app.errorhandler(KeyError)
    @app.errorhandler(TypeError)
    def bad(error):return jsonify(error=str(error)),400
    @app.errorhandler(HTTPException)
    def http_error(error):return jsonify(error=error.description),error.code
    @app.get('/health')
    def health():return jsonify(healthy=queue.thread is None or queue.thread.is_alive())
    @app.get('/')
    def index():return send_from_directory(Path(__file__).parent/'web','index.html')
    @app.get('/api/session')
    def csrf():
        if 'csrf' not in session:session['csrf']=secrets.token_hex(32)
        return jsonify(csrf=session['csrf'])
    @app.get('/api/books')
    def books():
        rows,total=queue.catalog(request.args.get('q',''))
        return jsonify(books=[{k:b[k] for k in ('id','title','author')} for b in rows],total=total)
    @app.get('/api/books/<int:book_id>/cover')
    def book_cover(book_id):return jsonify(cover=queue.cover_preview(book_id))
    @app.get('/api/profiles')
    def profiles():return jsonify(profiles=[{'name':k,'languages':v.get('languages',[])} for k,v in json.loads(queue.profiles.read_text()).items()],listen_url=listen_url)
    @app.get('/api/jobs')
    def jobs():return jsonify(jobs=queue.jobs())
    @app.post('/api/jobs')
    def submit():
        data=request.get_json() if request.is_json else request.form
        upload=request.files.get('cover') if not request.is_json else None
        cover=None
        if upload and upload.filename:
            cover=upload.stream.read(MAX_COVER_BYTES+1)
            if len(cover)>MAX_COVER_BYTES:raise ValueError('Cover image must be at most 5 MiB')
            if not cover:raise ValueError('Cover image is empty')
        return jsonify(queue.submit(data['book_id'],data['profile'],data.get('language','auto'),cover)),202
    @app.post('/api/jobs/<identity>/pause')
    def pause(identity):return jsonify(queue.pause(identity))
    @app.post('/api/jobs/<identity>/resume')
    def resume(identity):return jsonify(queue.resume(identity))
    return app

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='execute':
        r=json.loads(Path(sys.argv[2]).read_text())
        if r['version']!=runner.PIPELINE_VERSION:raise RuntimeError('Worker version changed')
        source=Path(sys.argv[3])/r['id']/'source.epub'
        if not source.exists():source=Path(r['source'])
        cover=Path(sys.argv[3])/r['id']/'cover-upload.jpg' if r.get('cover_uploaded') else None
        if cover is not None and not cover.is_file():raise RuntimeError('Uploaded cover is missing; restore the job state before resuming')
        identity=runner.convert(source,r['profile'],Path('/config/profiles.json'),Path(sys.argv[3]),Path(sys.argv[4]),r['language'],r['settings'],cover)
        if identity!=r['id']:raise RuntimeError('Job identity changed')
    else:
        from waitress import serve
        queue=Queue('/books','/state','/library','/config/profiles.json');queue.start()
        app=create_app(queue,os.environ['WEB_ORIGIN'],os.environ.get('WEB_BASE_PATH','/'),listen_url=os.environ.get('LISTEN_URL',''))
        def shutdown(*args):queue.stop();raise SystemExit(0)
        signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
        try:serve(app,host='0.0.0.0',port=8080,threads=4)
        finally:queue.stop()
