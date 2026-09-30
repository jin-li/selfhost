# Explicit local audiobook jobs

Build with `docker compose build audiobook web`, start the CPU TTS service with
`docker compose up -d kokoro`, and select one EPUB:

```sh
docker compose run --rm --no-deps audiobook convert /books/selected.epub --profile fast
```

Copy `.env.example` to a local `.env` and configure paths first. Provide a JSON
profile file at `PROFILES_FILE`, for example:

```json
{
  "fast": {
    "url": "http://kokoro:8880/v1",
    "model": "kokoro",
    "voice": "af_heart",
    "response_format": "pcm",
    "stream": true,
    "pcm_sample_rate": 24000,
    "max_chars": 300,
    "timeout": 300,
    "revision": "kokoro-fastapi-v0.2.2",
    "languages": ["en"]
  }
}
```

The pinned Kokoro release is used with raw signed 16-bit mono PCM because its
streaming WAV output can contain multiple WAV headers, and its non-streaming
path fails. Other servers default to complete WAV (`stream: false`). The worker
rejects inconsistent RIFF lengths before decoding. Requests to the same backend
are serialized across local jobs.

Other profiles point to private OpenAI-compatible `/v1/audio/speech` servers.
They can configure `language_map` and `voices_by_language`. `--language` defaults
to EPUB metadata. Add a profile without changing parsing or packaging code.
Never put API endpoints on a public listener. No paid/cloud fallback is present.

Use `status JOB_ID` and `resume JOB_ID` in place of `convert ...` to inspect or
resume a job. Repeating the same source/profile resumes too. Job identity includes
source, settings, language and pipeline version; concurrent duplicates serialize.
An upgrade changing the pipeline version refuses old-job resume: finish jobs first.

Inputs are read-only. Checkpointed PCM chunks have text and audio hashes. Transient
network/server failures are retried. ffmpeg validates and packages a chaptered
M4B with title, author and cover; a missing source cover gets a recorded fallback.
Output is published atomically in an author/book folder suitable for Audiobookshelf.
Successful jobs retain manifest/source/cover and remove temporary PCM/audio.

EPUB parsing uses EbookLib and BeautifulSoup, with a parser adapted from
[p0n1/epub_to_audiobook](https://github.com/p0n1/epub_to_audiobook) at
`b23f965881ace184c5a00c667bdbfe2117647b84`.
`m4b.py` is reused from
[davedavedavenm/epub-to-audiobook](https://github.com/davedavedavenm/epub-to-audiobook)
at `0380fec519fdfaa2bf0948ad1ad8f5fe4b28b2a1`; its runtime file selector accepts
lossless WAV chapters. Both MIT license notices are retained.

Run tests with `pytest test_runner.py test_integration.py` in an environment with
EbookLib, BeautifulSoup, Pillow, requests, pytest and ffmpeg/ffprobe. Fixture audio
checks restart recovery and packaging; a real synthesis test is still necessary
when adding/updating a model. Technical validation cannot guarantee a generative
voice pronounces every word correctly.

## Web queue

The optional `web` service provides Calibre EPUB search, profile selection,
a persistent one-at-a-time queue, progress, pause, and resume. It is a small
custom interface around the same converter, not a separate ebook/TTS engine.
Read-only `metadata.db` queries identify books; API requests select a book ID,
never an arbitrary filesystem path. Closing the browser does not stop a job.

Set `WEB_ORIGIN` to the external origin, `WEB_BASE_PATH` to its path prefix,
and `LISTEN_URL` to the audiobook server. Place it behind an authenticating
reverse proxy that supplies `Remote-User`, strips the path prefix, and redirects
the prefix without a trailing slash to its slash form. It fails closed without
the authentication header, uses CSRF tokens and same-origin POST checks, and
publishes no host port. Internal trusted Docker clients are part of this trust
boundary. Start with `docker compose up -d web kokoro`.

The CLI service has the `cli` Compose profile, preventing automatic conversion
when the stack starts. Explicit `docker compose run --rm audiobook ...` still
works. Queue records, a generated session signing key, logs, and queued source
snapshots live under `STATE_DIR/.web`; include this directory in state backups.
An interrupted running web job returns to the queue after server restart;
explicitly paused jobs stay paused. Pause may discard the in-flight chunk but
keeps completed checkpoints. Finished jobs remove the extra queued snapshot.
Only one web scheduler may use a state directory; a filesystem lock enforces it.

Run `pytest test_webapp.py` alongside the converter tests to exercise auth/CSRF,
path validation, durable deduplication, scheduler recovery, and real subprocess
pause/resume with ffmpeg packaging. Flask runs under Waitress following its
[production deployment guidance](https://flask.palletsprojects.com/en/stable/deploying/waitress/).
