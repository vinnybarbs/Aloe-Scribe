"""
win_smoke.py — Windows CI smoke test for the faster-whisper backend.

Transcribes a short committed clip (tests/sample.wav) through the real
FasterWhisperTranscriber and asserts it produced a non-empty, correctly
formatted transcript. Uses a tiny model by default (ALOE_TEST_MODEL) so CI is
fast; the production model is the GitHub-hosted distil-large-v3.

Run:  python tests/win_smoke.py
"""

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "src")
from transcriber_faster_whisper import FasterWhisperTranscriber

model = os.environ.get("ALOE_TEST_MODEL", "tiny.en")
wav = Path("tests/sample.wav")
out = Path("tests/_smoke_out.md")

print(f"Transcribing {wav} with model={model} ...")
t = FasterWhisperTranscriber(model=model, device="cpu", compute_type="int8")
res = t.transcribe(wav, out, "CI Smoke", datetime.now().astimezone())
assert res is not None, "transcribe() returned None"

text = out.read_text(encoding="utf-8")
print("----- transcript file -----")
print(text)

# Body after the YAML frontmatter must hold actual words.
body = text.split("---")[-1].strip()
assert body, "empty transcript body"
words = body.split()
assert len(words) >= 3, f"too few words transcribed: {body!r}"

# Header contract the downstream agent depends on.
assert "source: aloe-scribe-windows" in text, "missing/incorrect source line"

# Live-preview path: transcribe_samples on an in-memory array (what the rolling
# preview feeds). Exercises the same code on the real Windows runner.
import wave
import numpy as np

with wave.open(str(wav), "rb") as w:
    pcm = w.readframes(w.getnframes())
samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
preview = t.transcribe_samples(samples)
print("preview text:", repr(preview))
assert len(preview.split()) >= 3, f"preview produced too few words: {preview!r}"

print(f"SMOKE OK: {len(words)} words, header present, preview works")
