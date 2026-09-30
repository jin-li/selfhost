# Explicit local audiobook jobs

Build with `docker compose build audiobook`, start the CPU TTS service with
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
