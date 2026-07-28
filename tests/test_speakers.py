"""
test_speakers.py — unit tests for speaker attribution (src/speakers.py).

Covers the deterministic layer (channel assignment by energy, label
numbering, downmix, markdown/frontmatter output) with a synthetic stereo
WAV — no models needed. The sherpa-onnx diarization layer is exercised only
when RUN_DIARIZE=1 is set (downloads ~36 MB of models on first run).

Run:  .venv/bin/python tests/test_speakers.py
"""

import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import speakers
from frontmatter import build_frontmatter

RATE = 16000


def make_split_wav(path: Path, mic_bursts, sys_bursts, seconds=10.0):
    """Stereo WAV: white-noise bursts on each channel at the given
    (start, end) second spans."""
    n = int(seconds * RATE)
    rng = np.random.default_rng(7)

    def track(bursts):
        t = np.zeros(n, dtype=np.float32)
        for s, e in bursts:
            i0, i1 = int(s * RATE), int(e * RATE)
            t[i0:i1] = rng.standard_normal(i1 - i0).astype(np.float32) * 0.3
        return t

    mic = track(mic_bursts)
    system = track(sys_bursts)
    frames = np.stack(
        [(mic * 32767).astype(np.int16), (system * 32767).astype(np.int16)], axis=1
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames.tobytes())


def test_channel_labels(tmp: Path):
    wav = tmp / "split.wav"
    make_split_wav(
        wav,
        mic_bursts=[(0.0, 2.0), (6.0, 8.0)],
        sys_bursts=[(3.0, 5.0), (8.5, 9.5)],
    )
    assert speakers.wav_channels(wav) == 2

    sentences = [
        {"start": 0.2, "end": 1.8, "text": "hello from the room"},
        {"start": 3.2, "end": 4.8, "text": "hello from the remote side"},
        {"start": 6.2, "end": 7.8, "text": "the room again"},
        {"start": 8.6, "end": 9.4, "text": "remote again"},
    ]
    res = speakers.label_sentences(wav, sentences, diarizer=None)
    assert res is not None
    labeled, diarized = res
    assert diarized is False
    assert [s.label for s in labeled] == ["M1", "R1", "M1", "R1"], [
        s.label for s in labeled
    ]
    assert [s.text for s in labeled] == [s["text"] for s in sentences]

    body = speakers.build_labeled_body(labeled)
    assert "[00:00] M1: hello from the room" in body
    assert "[00:03] R1: hello from the remote side" in body

    extras = speakers.speaker_frontmatter_extras(labeled, diarized)
    fm = build_frontmatter("Test", __import__("datetime").datetime.now(), 10.0,
                           extras=extras)
    assert "speakers: [M1, R1]" in fm
    assert "speaker_key:" in fm
    assert "speaker_note:" in fm  # diarization did not run
    print("ok: channel labels")


def test_mono_passthrough(tmp: Path):
    """Mono WAVs are not split recordings — labeling must decline."""
    wav = tmp / "mono.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.zeros(RATE, dtype=np.int16).tobytes())
    assert speakers.label_sentences(
        wav, [{"start": 0, "end": 1, "text": "x"}], None
    ) is None
    print("ok: mono passthrough")


def test_downmix(tmp: Path):
    wav = tmp / "split2.wav"
    make_split_wav(wav, mic_bursts=[(0.0, 1.0)], sys_bursts=[(1.0, 2.0)], seconds=2.0)
    dst = tmp / "mono_out.wav"
    out = speakers.downmix_to_mono_wav(wav, dst)
    assert out == dst
    with wave.open(str(dst), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == RATE
        assert w.getnframes() == 2 * RATE
    # Downmix carries both bursts.
    data = np.frombuffer(dst.read_bytes()[44:], dtype=np.int16).astype(np.float32)
    assert np.abs(data[: RATE // 2]).mean() > 100  # mic burst present
    assert np.abs(data[RATE + RATE // 4 : 2 * RATE - RATE // 4]).mean() > 100
    print("ok: downmix")


def test_diarizer_optional(tmp: Path):
    if os.environ.get("RUN_DIARIZE") != "1":
        print("skip: diarizer (set RUN_DIARIZE=1 to run; downloads ~36 MB)")
        return
    wav = tmp / "split3.wav"
    make_split_wav(wav, mic_bursts=[(0.0, 3.0)], sys_bursts=[(4.0, 7.0)], seconds=8.0)
    d = speakers.Diarizer()
    mic, system, rate = speakers.load_split_channels(wav)
    turns = d.diarize(system)
    print(f"ok: diarizer ran, turns={turns}")


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_channel_labels(tmp)
        test_mono_passthrough(tmp)
        test_downmix(tmp)
        test_diarizer_optional(tmp)
    print("All speaker tests passed.")


if __name__ == "__main__":
    main()
