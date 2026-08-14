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


def test_tag_reconciliation():
    """Live tags name clusters; same name merges over-split clusters."""
    # (start, end, text, channel, cluster)
    tagged = [
        (0.0, 4.0, "hello", speakers.CH_SYSTEM, 0),
        (5.0, 9.0, "hi there", speakers.CH_MIC, 0),
        (10.0, 14.0, "more remote", speakers.CH_SYSTEM, 0),
        (15.0, 19.0, "over-split same voice", speakers.CH_SYSTEM, 1),
        (20.0, 24.0, "never tagged", speakers.CH_SYSTEM, 2),
    ]
    tags = [
        (2.0, "Brandon"),   # inside cluster S0 speech
        (6.5, "Vince"),     # inside mic speech
        (17.0, "brandon"),  # over-split cluster S1, case-insensitive
        (12.5, "Brandon"),  # second vote for S0
    ]
    names = speakers._names_from_tags(tagged, tags)
    assert names[(speakers.CH_SYSTEM, 0)] == "Brandon"
    assert names[(speakers.CH_SYSTEM, 1)] == "Brandon"  # merged by name
    assert names[(speakers.CH_MIC, 0)] == "Vince"
    assert (speakers.CH_SYSTEM, 2) not in names  # untagged stays anonymous

    # Trailing tolerance: tag typed just after the sentence ended still binds.
    late = speakers._names_from_tags(tagged, [(26.0, "Darren")])
    assert late[(speakers.CH_SYSTEM, 2)] == "Darren"
    # A tag in dead air far from any speech binds nothing.
    assert speakers._names_from_tags(tagged, [(60.0, "Ghost")]) == {}
    print("ok: tag reconciliation")


def test_attendees_frontmatter():
    labeled = [speakers.LabeledSentence(0.0, 1.0, "hi", "M1")]
    extras = speakers.speaker_frontmatter_extras(
        labeled, True, ["Vince Morello", "  ", "Roi, [x]"]
    )
    joined = "\n".join(extras)
    assert "attendees: [Vince Morello, Roi x]" in joined
    no_roster = speakers.speaker_frontmatter_extras(labeled, True, [])
    assert not any(line.startswith("attendees:") for line in no_roster)
    print("ok: attendees frontmatter")


def test_notes_section():
    notes = [(65.0, "Follow up with Alex on pricing"), (200, "  "), (10, "intro")]
    md = speakers.build_notes_section(notes)
    assert "## Notes" in md
    assert "[01:05] Follow up with Alex on pricing" in md
    assert "[00:10] intro" in md
    assert speakers.build_notes_section([]) == ""
    assert speakers.build_notes_section(None) == ""
    assert speakers.build_notes_section([(5, "   ")]) == ""
    print("ok: notes section")


def test_incremental_chunker(tmp: Path):
    """Chunks cut at silence, offsets applied, silent chunks skipped, tail
    finalized — with a fake transcriber, no models."""
    calls = []

    def fake_transcribe(path):
        with wave.open(str(path), "rb") as w:
            dur = w.getnframes() / RATE
        calls.append(round(dur, 1))
        return [{"start": 0.5, "end": min(2.0, dur), "text": f"chunk{len(calls)}"}]

    ch = speakers.IncrementalChunker(fake_transcribe, tmp_dir=tmp)
    rng = np.random.default_rng(3)

    def stereo_bytes(mic_loud, sys_loud, seconds):
        n = int(seconds * RATE)
        mk = lambda loud: (
            (rng.standard_normal(n) * (0.2 if loud else 0.0005) * 32767)
        ).astype(np.int16)
        return np.stack([mk(mic_loud), mk(sys_loud)], axis=1).tobytes()

    # 60 s: below chunk size — nothing processes yet.
    ch.feed(stereo_bytes(True, False, 60), 2)
    assert ch.maybe_process() is False and calls == []
    # +40 s more mic speech: mic chunk (~90 s) processes; system stays quiet.
    ch.feed(stereo_bytes(True, False, 40), 2)
    assert ch.maybe_process() is True
    assert len(calls) == 1 and 74 <= calls[0] <= 90  # cut inside search window
    first = ch.sentences[speakers.CH_MIC][0]
    assert first["start"] == 0.5  # first chunk, zero offset
    # Drain the silent system channel's due chunk (skipped, no model call).
    while ch.maybe_process():
        pass
    n_calls = len(calls)
    # Finalize: mic tail transcribes with the chunk offset applied.
    result = ch.finalize()
    assert len(calls) == n_calls + 1
    tail = result[speakers.CH_MIC][-1]
    assert tail["start"] > 70  # offset carried into the tail sentences
    assert result[speakers.CH_SYSTEM] == []  # silence never transcribed
    print("ok: incremental chunker")


def test_assemble_labeled(tmp: Path):
    """assemble_labeled_transcript renders from pre-transcribed sentences
    (no STT), with tags naming and notes appended."""
    wav = tmp / "asm.wav"
    make_split_wav(wav, mic_bursts=[(0.0, 3.0)], sys_bursts=[(4.0, 8.0)], seconds=9.0)
    per_channel = {
        speakers.CH_MIC: [{"start": 0.2, "end": 2.8, "text": "hello from mic"}],
        speakers.CH_SYSTEM: [{"start": 4.2, "end": 7.8, "text": "hello from remote"}],
    }
    md = speakers.assemble_labeled_transcript(
        wav, per_channel, None, "T", __import__("datetime").datetime.now(),
        "aloe-scribe-mac", tags=[(1.0, "Vince")], notes=[(5, "note here")],
    )
    assert "[00:00] Vince: hello from mic" in md
    assert "R1: hello from remote" in md
    assert "## Notes" in md and "note here" in md
    print("ok: assemble labeled")


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

    # Chip renames arrive ONE AT A TIME. A case-variant of a name already in
    # the document must merge into it, and speaker_channels must not stack
    # duplicate lines across successive renames.
    step1 = speakers.apply_speaker_names(md, {"R1": "Priya Khan"})
    step2 = speakers.apply_speaker_names(step1, {"R2": "priya khan"})
    assert "[00:12] Priya Khan: brief" in step2  # merged, existing casing wins
    assert "priya khan:" not in step2
    assert "speakers: [M1, Priya Khan]" in step2
    assert step2.count("speaker_channels:") == 1
    assert 'speaker_channels: "Priya Khan=R1; Priya Khan=R2"' in step2
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
        test_tag_reconciliation()
        test_incremental_chunker(tmp)
        test_assemble_labeled(tmp)
        test_attendees_frontmatter()
        test_notes_section()
        test_speaker_naming()
        test_mono_passthrough(tmp)
        test_downmix(tmp)
        test_diarizer_optional(tmp)
    print("All speaker tests passed.")


if __name__ == "__main__":
    main()
