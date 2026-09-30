FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends calibre ffmpeg fonts-dejavu-core fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir EbookLib==0.19 beautifulsoup4==4.13.4 lxml==5.4.0 requests==2.32.4 Pillow==11.2.1 Flask==3.1.3 waitress==3.0.2
WORKDIR /app
COPY runner.py m4b.py webapp.py LICENSE.* ./
COPY web ./web
ENTRYPOINT ["python", "/app/runner.py"]
