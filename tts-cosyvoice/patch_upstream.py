"""Checked compatibility edits for the pinned upstream on ROCm / torchaudio 2.9."""
from pathlib import Path

root = Path('/opt/CosyVoice')
p = root / 'cosyvoice/cli/frontend.py'
s = p.read_text()
old = '"CUDAExecutionProvider" if torch.cuda.is_available() else\n                                                                                "CPUExecutionProvider"'
assert s.count(old) == 1
p.write_text(s.replace(old, '"CPUExecutionProvider"'))
p = root / 'cosyvoice/utils/file_utils.py'
s = p.read_text()
old = "speech, sample_rate = torchaudio.load(wav, backend='soundfile')"
assert s.count(old) == 1
p.write_text(s.replace(old, "import soundfile as sf\n    data, sample_rate = sf.read(wav, dtype='float32', always_2d=True)\n    speech = torch.from_numpy(data.T.copy())"))
# Requests always use text_frontend=False: do not fetch optional, unpinned
# ttsfrd/wetext models while constructing the upstream frontend.
p = root / 'cosyvoice/cli/frontend.py'
s = p.read_text()
start = s.index('        # NOTE compatible when no text frontend tool is avaliable')
end = s.index('    def _extract_text_token', start)
assert 'from wetext import Normalizer' in s[start:end]
s = s[:start] + "        self.text_frontend = ''\n\n\n" + s[end:]
p.write_text(s)
