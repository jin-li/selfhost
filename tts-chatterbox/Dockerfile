FROM rocm/pytorch:rocm7.2_ubuntu24.04_py3.12_pytorch_release_2.9.1@sha256:a3b65813621095e3389269417e963725b59310184588c9d2490d44e6e83fa01c
ARG SERVER_REV=915ae289340e10c6047f27f47e22eae9bf350c32
ARG ENGINE_REV=cc0357396d9c73fc1e6c544ee40bb596020edd09
ENV PYTHONUNBUFFERED=1 HF_HOME=/cache HF_HUB_DISABLE_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 git && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/devnen/Chatterbox-TTS-Server.git /app && cd /app && git checkout "$SERVER_REV" && rm -rf .git
WORKDIR /app
# The base records direct-URL wheels; pip freeze does not emit name==version.
RUN python -c 'import importlib.metadata as m; print("\n".join(n+"=="+m.version(n) for n in ("torch","torchaudio","torchvision","triton")))' > /tmp/torch-constraints.txt && \
    python -m pip install -c /tmp/torch-constraints.txt -r requirements-rocm.txt watchdog unidecode inflect audiotsm && \
    python -m pip install --no-deps "git+https://github.com/devnen/chatterbox-v2.git@$ENGINE_REV" s3tokenizer==0.3.0 onnx==1.16.0 && \
    python -m pip install 'protobuf>=4.25.0'
RUN python - <<'PY'
import pathlib, chatterbox
base = pathlib.Path(chatterbox.__file__).parent
mtl = base / "mtl_tts.py"
mtl_text = mtl.read_text()
assert 'revision="main"' in mtl_text, "Model revision patch no longer matches"
mtl.write_text(mtl_text.replace('revision="main"', 'revision="5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18"'))
fixes = {
 'models/s3tokenizer/s3tokenizer.py': [('wav = torch.from_numpy(wav)', 'wav = torch.from_numpy(wav).float()'), ('self._mel_filters.to(self.device) @ magnitudes', 'self._mel_filters.to(self.device).to(magnitudes.dtype) @ magnitudes')]
}
for name, replacements in fixes.items():
 p = base / name
 text = p.read_text()
 for old, new in replacements:
  assert old in text, f"Dtype patch no longer matches {name}"
  text = text.replace(old, new)
 p.write_text(text)
# The pinned engine already applies the voice-encoder float32 correction.
assert "mels.to(self.device, dtype=torch.float32)" in (base / "models/voice_encoder/voice_encoder.py").read_text()
PY
# Language helpers are omitted by the upstream ROCm requirements list.
RUN python -m pip install -c /tmp/torch-constraints.txt librosa==0.11.0 spacy-pkuseg==1.0.1 pykakasi==2.3.0 && mkdir -p /tmp/outputs
COPY patch_engine.py /tmp/patch_engine.py
RUN python /tmp/patch_engine.py
EXPOSE 8004
CMD ["python", "server.py"]
