"""
speakers.py — speaker attribution for Aloe Scribe transcripts.

Works on the split (stereo) WAV the recorders produce when both sources are
active: channel 0 is the local mic, channel 1 is system audio (remote
participants). The two channels are sample-aligned, so transcript sentence
timestamps line up with both.

Attribution happens in two layers:

1. Channel: each channel is transcribed SEPARATELY, so which side a sentence
   came from is a structural fact, not a guess — and speech that overlaps
   across the two sides (people talking over each other) is fully captured
   instead of colliding in a mono downmix. Mic sentences that are acoustic
   echo of remote speech (no-AEC setups) are deduped against the system
   channel. Near-silent channels are skipped entirely.
2. Speaker: each channel is run through offline diarization (sherpa-onnx,
   pyannote segmentation + CAM++ speaker embeddings) to split multiple
   voices on the same side. Diarization is optional — if sherpa-onnx or its
   models are unavailable the transcript still gets channel-level labels.

Labels are neutral on purpose: M1, M2, ... for voices on the mic side and
R1, R2, ... for voices on the system side. The mic side is NOT assumed to be
the machine's owner — a laptop recording a conference room hears several
people on M*. The downstream summary agent maps labels to names using the
calendar event and context; the frontmatter carries a key explaining the
scheme so it can do that without guessing the format.

Diarization models (~36 MB total) are downloaded once from the sherpa-onnx
GitHub releases and cached under ~/.cache/aloe-scribe/diarization/.
"""

import logging
import tarfile
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Channel order in the split WAV (fixed contract with both recorders).
CH_MIC = 0
CH_SYSTEM = 1

_LABEL_PREFIX = {CH_MIC: "M", CH_SYSTEM: "R"}

# sherpa-onnx model assets. Note: "recongition" is the real (typo'd) tag name
# on the k2-fsa release — do not "fix" it.
_SEG_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
_SEG_SUBPATH = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
_EMB_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
)
_EMB_FILENAME = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "aloe-scribe" / "diarization"


# ---------------------------------------------------------------------------
# WAV helpers
# ---------------------------------------------------------------------------

def wav_channels(path: Path) -> int:
    """Channel count of a WAV file (0 on failure)."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnchannels()
    except Exception as e:
        log.debug(f"wav_channels({path}): {e}")
        return 0


def load_split_channels(path: Path):
    """
    Load a stereo split WAV. Returns (mic, system, sample_rate) where mic and
    system are float32 numpy arrays in [-1, 1]. Returns None if the file is
    not a 2-channel 16-bit WAV.
    """
    import numpy as np

    try:
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 2 or w.getsampwidth() != 2:
                return None
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception as e:
        log.warning(f"Could not read split WAV {path}: {e}")
        return None

    data = np.frombuffer(raw, dtype=np.int16)
    data = data[: (len(data) // 2) * 2].reshape(-1, 2)
    mic = data[:, CH_MIC].astype(np.float32) / 32768.0
    system = data[:, CH_SYSTEM].astype(np.float32) / 32768.0
    return mic, system, rate


def downmix_to_mono_wav(src: Path, dst: Path) -> Optional[Path]:
    """
    Write a mono 16-bit WAV downmix of `src` to `dst` (saturating sum, same
    loudness as the pre-split mixed output). Mono sources are copied as-is.
    Used by recovery/batch paths whose STT backend expects mono.
    """
    import numpy as np

    try:
        with wave.open(str(src), "rb") as w:
            channels = w.getnchannels()
            rate = w.getframerate()
            width = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if channels == 1:
            dst.write_bytes(src.read_bytes())
            return dst
        if width != 2:
            return None
        data = np.frombuffer(raw, dtype=np.int16)
        data = data[: (len(data) // channels) * channels].reshape(-1, channels)
        mixed = np.clip(
            data.astype(np.int32).sum(axis=1), -32768, 32767
        ).astype(np.int16)
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(mixed.tobytes())
        return dst
    except Exception as e:
        log.error(f"Downmix failed for {src}: {e}")
        return None


# ---------------------------------------------------------------------------
# Diarization (optional, sherpa-onnx)
# ---------------------------------------------------------------------------

def _download(url: str, dst: Path) -> bool:
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        log.info(f"Downloading diarization model: {url}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(dst)
        return True
    except Exception as e:
        log.warning(f"Model download failed ({url}): {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


class Diarizer:
    """
    Offline speaker diarization via sherpa-onnx. All failure modes (package
    missing, download blocked, model init error) degrade to unavailable —
    callers then fall back to channel-only labels.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or default_cache_dir()
        self._sd = None
        self._failed = False

    def _model_paths(self) -> Optional[tuple]:
        seg = self.cache_dir / "pyannote-segmentation-3-0.onnx"
        emb = self.cache_dir / _EMB_FILENAME
        if not seg.exists():
            tar_path = self.cache_dir / "segmentation.tar.bz2"
            if not _download(_SEG_URL, tar_path):
                return None
            try:
                with tarfile.open(tar_path, "r:bz2") as tf:
                    member = tf.getmember(_SEG_SUBPATH)
                    with tf.extractfile(member) as f:
                        seg.write_bytes(f.read())
                tar_path.unlink(missing_ok=True)
            except Exception as e:
                log.warning(f"Could not extract segmentation model: {e}")
                return None
        if not emb.exists():
            if not _download(_EMB_URL, emb):
                return None
        return seg, emb

    def _ensure_ready(self) -> bool:
        if self._sd is not None:
            return True
        if self._failed:
            return False
        try:
            import sherpa_onnx
        except ImportError:
            log.info(
                "sherpa-onnx not installed — speaker labels will be "
                "channel-level only (pip install sherpa-onnx to split "
                "individual voices)"
            )
            self._failed = True
            return False
        paths = self._model_paths()
        if paths is None:
            self._failed = True
            return False
        seg, emb = paths
        try:
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=str(seg)
                    ),
                ),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb)),
                # Threshold calibrated on real meeting audio, not the 0.5 the
                # sherpa examples use: Teams/Zoom codec compression distorts
                # voice embeddings enough that same-speaker distances inflate.
                # At 0.5 a 23-min 6-person call fragmented into 32 "speakers";
                # 1.0 recovered the true count. Higher merges real speakers
                # (1.25 collapsed everyone into one).
                clustering=sherpa_onnx.FastClusteringConfig(
                    num_clusters=-1, threshold=1.0
                ),
                min_duration_on=0.3,
                min_duration_off=0.5,
            )
            self._sd = sherpa_onnx.OfflineSpeakerDiarization(config)
        except Exception as e:
            log.warning(f"Diarizer init failed: {e}")
            self._failed = True
            return False
        return True

    def diarize(self, samples) -> Optional[list]:
        """
        samples: float32 mono 16 kHz numpy array.
        Returns [(start_sec, end_sec, cluster_id), ...] sorted by start, or
        None if diarization is unavailable/failed.
        """
        if not self._ensure_ready():
            return None
        try:
            result = self._sd.process(samples).sort_by_start_time()
            return [(s.start, s.end, int(s.speaker)) for s in result]
        except Exception as e:
            log.warning(f"Diarization failed: {e}")
            return None


# ---------------------------------------------------------------------------
# Sentence labeling
# ---------------------------------------------------------------------------

@dataclass
class LabeledSentence:
    start: float
    end: float
    text: str
    label: str          # "M1", "R2", ...


def _sentence_fields(s) -> Optional[tuple]:
    """Accept objects with .start/.end/.text or dicts with those keys."""
    if isinstance(s, dict):
        start, end, text = s.get("start"), s.get("end"), s.get("text")
    else:
        start = getattr(s, "start", None)
        end = getattr(s, "end", None)
        text = getattr(s, "text", None)
    text = (text or "").strip()
    if not text or start is None:
        return None
    start = float(start or 0.0)
    end = float(end if end is not None else start)
    if end < start:
        end = start
    return start, end, text


def _overlap_speaker(turns, start: float, end: float) -> Optional[int]:
    """Cluster id with the largest time overlap with [start, end], or the
    nearest turn if nothing overlaps (short sentences between VAD gaps)."""
    if not turns:
        return None
    best_id, best_ov = None, 0.0
    nearest_id, nearest_gap = None, float("inf")
    for t_start, t_end, spk in turns:
        ov = min(end, t_end) - max(start, t_start)
        if ov > best_ov:
            best_ov, best_id = ov, spk
        gap = max(t_start - end, start - t_end, 0.0)
        if gap < nearest_gap:
            nearest_gap, nearest_id = gap, spk
    return best_id if best_id is not None else nearest_id


def activity_fraction(samples, rate: int) -> float:
    """Fraction of 1-second windows with speech-level energy. Used to skip
    transcribing a dead channel (e.g. a hardware-muted Jabra)."""
    import numpy as np

    n = len(samples) // rate
    if n == 0:
        return 0.0
    w = samples[: n * rate].reshape(n, rate)
    rms = np.sqrt((w.astype(np.float64) ** 2).mean(axis=1))
    return float((rms > 0.0045).mean())


def write_channel_wav(samples, rate: int, dst: Path) -> Path:
    """Write one float32 channel back out as a mono int16 WAV for the STT
    backend."""
    import numpy as np

    data = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return dst


def dedupe_echo(mic_sentences: list, sys_sentences: list) -> list:
    """Drop mic-channel sentences that are acoustic echo of remote speech.

    On setups without echo cancellation (laptop speakers + built-in mic) the
    mic hears the remote participants too, so the same words appear on both
    channels at the same time. A mic sentence that overlaps a system sentence
    in time AND says nearly the same thing is the speaker's echo, not the
    local person — keep the clean system copy only.
    """
    import difflib

    out = []
    for m in mic_sentences:
        dur = max(0.2, m[1] - m[0])
        is_echo = False
        for s in sys_sentences:
            overlap = min(m[1], s[1]) - max(m[0], s[0])
            if overlap / dur < 0.5:
                continue
            sim = difflib.SequenceMatcher(
                None, m[2].lower(), s[2].lower()
            ).ratio()
            if sim > 0.65:
                is_echo = True
                break
        if not is_echo:
            out.append(m)
    return out


def _parse_sentences(sentences) -> list:
    parsed = []
    for s in sentences or []:
        f = _sentence_fields(s)
        if f:
            parsed.append(f)
    parsed.sort(key=lambda f: f[0])
    return parsed


def build_labeled_transcript(
    wav_path: Path,
    transcribe_sentences,
    diarizer: Optional[Diarizer],
    title: str,
    when,
    source: str,
) -> Optional[str]:
    """
    Full speaker-labeled transcript from a split (stereo) recording.

    Each channel is transcribed SEPARATELY — the channels are clean single-
    party audio, so overlapping speech (two people talking at once) is fully
    recovered instead of fighting for one mono downmix, and the mic/remote
    attribution is structural rather than an energy guess. Near-silent
    channels are skipped. Voices within a channel are split by diarization.

    `transcribe_sentences`: callable(Path) -> list of {'start','end','text'}
    (the backend's batch API). Returns the markdown string, or None when the
    WAV is not a split recording or nothing was transcribed — callers fall
    back to the plain downmix path.
    """
    from frontmatter import build_frontmatter

    loaded = load_split_channels(wav_path)
    if loaded is None:
        return None
    mic, system, rate = loaded

    per_channel: dict = {}
    tmp_files = []
    try:
        for channel, samples in ((CH_MIC, mic), (CH_SYSTEM, system)):
            if activity_fraction(samples, rate) < 0.005:
                log.info(
                    f"Channel {channel} ({'mic' if channel == CH_MIC else 'system'}) "
                    "is silent — skipping its transcription"
                )
                per_channel[channel] = []
                continue
            tmp = wav_path.with_name(f".{wav_path.stem}.ch{channel}.wav")
            write_channel_wav(samples, rate, tmp)
            tmp_files.append(tmp)
            sentences = transcribe_sentences(tmp)
            per_channel[channel] = _parse_sentences(sentences)
    finally:
        for t in tmp_files:
            try:
                t.unlink()
            except Exception:
                pass

    per_channel[CH_MIC] = dedupe_echo(per_channel[CH_MIC], per_channel[CH_SYSTEM])
    if not per_channel[CH_MIC] and not per_channel[CH_SYSTEM]:
        return None

    turns = {CH_MIC: None, CH_SYSTEM: None}
    if diarizer is not None:
        if per_channel[CH_MIC]:
            turns[CH_MIC] = diarizer.diarize(mic)
        if per_channel[CH_SYSTEM]:
            turns[CH_SYSTEM] = diarizer.diarize(system)

    # Tag each sentence with (channel, cluster), merge both channels by time,
    # then number labels by order of first appearance.
    tagged = []
    for channel in (CH_MIC, CH_SYSTEM):
        for start, end, text in per_channel[channel]:
            cluster = (
                _overlap_speaker(turns[channel], start, end)
                if turns[channel]
                else 0
            )
            tagged.append((start, end, text, channel, 0 if cluster is None else cluster))
    tagged.sort(key=lambda t: t[0])

    label_map: dict = {}
    counts = {CH_MIC: 0, CH_SYSTEM: 0}
    labeled = []
    for start, end, text, channel, cluster in tagged:
        key = (channel, cluster)
        if key not in label_map:
            counts[channel] += 1
            label_map[key] = f"{_LABEL_PREFIX[channel]}{counts[channel]}"
        labeled.append(
            LabeledSentence(start=start, end=end, text=text, label=label_map[key])
        )

    diarized = any(t is not None for t in turns.values())
    duration = max((s.end for s in labeled), default=0.0)
    fm = build_frontmatter(
        title,
        when,
        duration,
        source=source,
        extras=speaker_frontmatter_extras(labeled, diarized),
    )
    return fm + "\n\n" + build_labeled_body(labeled) + "\n"


def speaker_frontmatter_extras(labeled: list, diarized: bool) -> list:
    """Frontmatter lines describing the labeling scheme for the summary agent."""
    seen = []
    for s in labeled:
        if s.label not in seen:
            seen.append(s.label)
    scheme = (
        "M* = voices on the local microphone (the in-room side; may be one or "
        "several people, not necessarily the machine's owner). R* = voices on "
        "system audio (remote participants). Match labels to names using the "
        "calendar event and conversational context."
    )
    lines = [
        f"speakers: [{', '.join(seen)}]",
        f'speaker_key: "{scheme}"',
    ]
    if not diarized:
        lines.append(
            'speaker_note: "Diarization unavailable for this recording — each '
            'label is a channel, and may lump several voices together."'
        )
    return lines


_LINE_RE = r"^\[(\d+):(\d\d)\] ([A-Za-z][\w .'-]*?): (.*)$"


def speaker_quotes(md_text: str) -> list:
    """From a labeled transcript, one entry per speaker in order of first
    appearance: (label, representative_quote, line_count). The quote is the
    speaker's longest line (most identifiable), trimmed for display. Used by
    the post-recording "who was speaking?" prompt."""
    import re

    by_label: dict = {}
    order = []
    for line in md_text.splitlines():
        m = re.match(_LINE_RE, line)
        if not m:
            continue
        label, text = m.group(3), m.group(4).strip()
        if label not in by_label:
            by_label[label] = []
            order.append(label)
        by_label[label].append(text)
    out = []
    for label in order:
        lines = by_label[label]
        quote = max(lines, key=len)
        if len(quote) > 140:
            quote = quote[:137].rsplit(" ", 1)[0] + "…"
        out.append((label, quote, len(lines)))
    return out


def _sanitize_name(name: str) -> str:
    """Names go into YAML lists and transcript lines — keep them plain."""
    cleaned = "".join(c for c in name if c not in "[]{}:,\"'\n\t")
    return " ".join(cleaned.split())[:40]


def apply_speaker_names(md_text: str, mapping: dict) -> str:
    """Rewrite a labeled transcript with human names.

    `mapping`: {label: name}. Blank/missing names keep their label. The same
    name on two labels merges those speakers. The frontmatter `speakers:`
    list is rewritten to the final names, and a `speaker_channels:` line
    records which side each name was heard on so the downstream agent keeps
    the local/remote distinction.
    """
    import re

    mapping = {
        label: _sanitize_name(name)
        for label, name in (mapping or {}).items()
        if name and _sanitize_name(name)
    }
    if not mapping:
        return md_text

    out_lines = []
    final_order = []
    channel_notes = []
    seen = set()
    for line in md_text.splitlines():
        m = re.match(_LINE_RE, line)
        if m:
            label = m.group(3)
            name = mapping.get(label, label)
            if name not in seen:
                seen.add(name)
                final_order.append(name)
                if label != name:
                    channel_notes.append(f"{name}={label}")
            line = f"[{m.group(1)}:{m.group(2)}] {name}: {m.group(4)}"
        out_lines.append(line)

    text = "\n".join(out_lines)
    if final_order:
        text = re.sub(
            r"^speakers: \[[^\]]*\]$",
            f"speakers: [{', '.join(final_order)}]",
            text,
            count=1,
            flags=re.M,
        )
    if channel_notes:
        text = re.sub(
            r"^(speakers: \[[^\]]*\])$",
            r"\1" + f"\nspeaker_channels: \"{'; '.join(channel_notes)}\"",
            text,
            count=1,
            flags=re.M,
        )
    if md_text.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text


def build_labeled_body(labeled: list) -> str:
    """Transcript body: one '[MM:SS] LABEL: text' line per sentence."""
    lines = []
    for s in labeled:
        m, sec = divmod(int(s.start), 60)
        lines.append(f"[{m:02d}:{sec:02d}] {s.label}: {s.text}")
    return "\n".join(lines)
