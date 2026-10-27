FROM rocm/pytorch:rocm7.2_ubuntu24.04_py3.12_pytorch_release_2.9.1@sha256:a3b65813621095e3389269417e963725b59310184588c9d2490d44e6e83fa01c
ARG COSYVOICE_REV=074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
ENV PYTHONUNBUFFERED=1 HF_HOME=/cache/huggingface HF_HUB_DISABLE_TELEMETRY=1 PYTHONPATH=/opt/CosyVoice:/opt/CosyVoice/third_party/Matcha-TTS
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg libsndfile1 sox && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/QwenAudio/CosyVoice.git /opt/CosyVoice && cd /opt/CosyVoice && git checkout "$COSYVOICE_REV" && git submodule update --init --recursive && rm -rf .git
WORKDIR /opt/CosyVoice
# Keep AMD's exact torch wheels. Do not install upstream's CUDA requirements.
RUN python -c 'import importlib.metadata as m; print("\n".join(n+"=="+m.version(n) for n in ("torch","torchaudio","torchvision","triton")))' > /tmp/torch-constraints.txt
COPY requirements.txt /app/requirements.txt
RUN printf "setuptools==80.9.0\n" > /tmp/build-constraints.txt && python -m pip install --build-constraint /tmp/build-constraints.txt -c /tmp/torch-constraints.txt -r /app/requirements.txt
COPY patch_upstream.py /app/patch_upstream.py
RUN python /app/patch_upstream.py && python -c 'from cosyvoice.cli.cosyvoice import AutoModel'
COPY bootstrap.py server.py /app/
COPY tts_runtime.py /app/tts_runtime.py
EXPOSE 8007
CMD ["python", "/app/bootstrap.py"]
