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


def test_per_channel_labels(tmp: Path):
    """Per-channel transcription: overlapping mic+system speech both survive,
    channels label structurally, merged output is time-ordered."""
    from datetime import datetime

    wav = tmp / "split.wav"
    # Note 6.0-8.0: BOTH sides talk at once (crosstalk) — the whole point of
    # per-channel transcription is that neither side is lost.
    make_split_wav(
        wav,
        mic_bursts=[(0.0, 2.0), (6.0, 8.0)],
        sys_bursts=[(3.0, 5.0), (6.0, 8.0), (8.5, 9.5)],
    )
    assert speakers.wav_channels(wav) == 2

    calls = []

    def fake_transcribe(path: Path):
        calls.append(path.name)
        if ".ch0." in path.name:  # mic
            return [
                {"start": 0.2, "end": 1.8, "text": "hello from the room"},
                {"start": 6.1, "end": 7.9, "text": "the room talking over"},
            ]
        return [
            {"start": 3.2, "end": 4.8, "text": "hello from the remote side"},
            {"start": 6.0, "end": 7.8, "text": "remote talking at the same time"},
            {"start": 8.6, "end": 9.4, "text": "remote again"},
        ]

    md = speakers.build_labeled_transcript(
        wav, fake_transcribe, None, "Test", datetime.now(), "aloe-scribe-mac"
    )
    assert md is not None
    assert len(calls) == 2, calls  # both channels transcribed
    assert "[00:00] M1: hello from the room" in md
    assert "[00:03] R1: hello from the remote side" in md
    # Crosstalk: both sides present around 6-8 s
    assert "M1: the room talking over" in md
    assert "R1: remote talking at the same time" in md
    assert "speakers: [M1, R1]" in md
    assert "speaker_note:" in md  # diarizer=None → channel-level only
    # Time-ordered merge
    assert md.index("hello from the room") < md.index("hello from the remote side")
    print("ok: per-channel labels + crosstalk")


def test_silent_channel_skipped(tmp: Path):
    """A dead (muted-mic) channel must not be transcribed at all."""
    from datetime import datetime

    wav = tmp / "muted.wav"
    make_split_wav(wav, mic_bursts=[], sys_bursts=[(0.0, 6.0)], seconds=8.0)

    calls = []

    def fake_transcribe(path: Path):
        calls.append(path.name)
        return [{"start": 0.5, "end": 5.5, "text": "only remote speech"}]

    md = speakers.build_labeled_transcript(
        wav, fake_transcribe, None, "Test", datetime.now(), "aloe-scribe-mac"
    )
    assert md is not None
    assert len(calls) == 1 and ".ch1." in calls[0], calls
    assert "R1: only remote speech" in md
    assert "M1" not in md
    print("ok: silent channel skipped")


def test_echo_dedupe():
    """Mic sentences that duplicate remote speech in time AND words are echo."""
    sys_s = [(10.0, 13.0, "we should review the quarterly numbers")]
    mic_s = [
        (10.2, 13.1, "we should review the quarterly numbers"),  # echo → drop
        (14.0, 15.0, "yes I agree completely"),                  # real → keep
    ]
    kept = speakers.dedupe_echo(mic_s, sys_s)
    assert [k[2] for k in kept] == ["yes I agree completely"], kept
    print("ok: echo dedupe")


def test_speaker_naming():
    """Quote extraction and name rewriting for the post-recording prompt."""
    md = "\n".join([
        "---",
        'title: "T"',
        "speakers: [M1, R1, R2]",
        'speaker_key: "M* = mic side"',
        "---",
        "",
        "[00:01] M1: short",
        "[00:05] R1: this is the longest most identifiable line from R one",
        "[00:09] M1: a much longer mic line that should be the chosen quote",
        "[00:12] R2: brief",
        "[00:15] R1: shorter line",
        "",
    ])
    quotes = speakers.speaker_quotes(md)
    assert [q[0] for q in quotes] == ["M1", "R1", "R2"]
    # M1: first + longest (also last) → two distinct quotes, chronological
    assert quotes[0][1] == [
        "short",
        "a much longer mic line that should be the chosen quote",
    ]
    assert quotes[0][2] == 2  # M1 spoke twice
    assert quotes[2][1] == ["brief"]  # single-line speaker → one quote

    named = speakers.apply_speaker_names(md, {"M1": "Vincent", "R1": "Priya"})
    assert "[00:01] Vincent: short" in named
    assert "[00:05] Priya: this is the longest" in named
    assert "[00:12] R2: brief" in named  # unnamed keeps label
    assert "speakers: [Vincent, Priya, R2]" in named
    assert 'speaker_channels: "Vincent=M1; Priya=R1"' in named

    # Same name on two labels merges them in the speakers list.
    merged = speakers.apply_speaker_names(md, {"R1": "Sam", "R2": "Sam"})
    assert "speakers: [M1, Sam]" in merged
    assert "[00:12] Sam: brief" in merged

    # Hostile input is sanitized; empty mapping is a no-op.
    weird = speakers.apply_speaker_names(md, {"M1": '  Bob, [x]: "q"  '})
    assert "[00:01] Bob x q: short" in weird
    assert speakers.apply_speaker_names(md, {}) == md
    assert speakers.apply_speaker_names(md, {"M1": "   "}) == md
    print("ok: speaker naming")


def test_mono_passthrough(tmp: Path):
    """Mono WAVs are not split recordings — labeling must decline."""
    from datetime import datetime

    wav = tmp / "mono.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.zeros(RATE, dtype=np.int16).tobytes())
    md = speakers.build_labeled_transcript(
        wav, lambda p: [], None, "Test", datetime.now(), "aloe-scribe-mac"
    )
    assert md is None
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
        test_per_channel_labels(tmp)
        test_silent_channel_skipped(tmp)
        test_echo_dedupe()
        test_speaker_naming()
        test_mono_passthrough(tmp)
        test_downmix(tmp)
        test_diarizer_optional(tmp)
    print("All speaker tests passed.")


if __name__ == "__main__":
    main()
