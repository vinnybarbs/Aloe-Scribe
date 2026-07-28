"""
speakers.py — speaker attribution for Aloe Scribe transcripts.

Works on the split (stereo) WAV the recorders produce when both sources are
active: channel 0 is the local mic, channel 1 is system audio (remote
participants). The two channels are sample-aligned, so transcript sentence
timestamps line up with both.

Attribution happens in two layers:

1. Channel: each transcript sentence is assigned to the mic side or the
   system side by comparing RMS energy across its time span. This is
   deterministic and needs no models.
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


def _rms(arr) -> float:
    import numpy as np

    if arr is None or len(arr) == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))


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


def label_sentences(
    wav_path: Path,
    sentences,
    diarizer: Optional[Diarizer] = None,
) -> Optional[tuple]:
    """
    Attribute transcript sentences to speakers using the split WAV.

    Returns (labeled, diarized): a list of LabeledSentence sorted by start,
    and whether per-voice diarization actually ran (False = channel-level
    labels only). Returns None when the WAV is not a split recording — the
    caller keeps its unlabeled transcript. `diarizer=None` still yields
    channel-level labels (all mic speech = M1, all system speech = R1).
    """
    loaded = load_split_channels(wav_path)
    if loaded is None:
        return None
    mic, system, rate = loaded

    parsed = []
    for s in sentences or []:
        f = _sentence_fields(s)
        if f:
            parsed.append(f)
    if not parsed:
        return None
    parsed.sort(key=lambda f: f[0])

    turns = {CH_MIC: None, CH_SYSTEM: None}
    if diarizer is not None:
        turns[CH_MIC] = diarizer.diarize(mic)
        turns[CH_SYSTEM] = diarizer.diarize(system)

    # Cluster → final label, numbered by order of first appearance per side.
    label_map: dict = {}
    counts = {CH_MIC: 0, CH_SYSTEM: 0}

    def label_for(channel: int, cluster) -> str:
        key = (channel, cluster)
        if key not in label_map:
            counts[channel] += 1
            label_map[key] = f"{_LABEL_PREFIX[channel]}{counts[channel]}"
        return label_map[key]

    out = []
    n = len(mic)
    for start, end, text in parsed:
        i0 = max(0, min(n, int(start * rate)))
        i1 = max(i0 + 1, min(n, int(end * rate) or i0 + 1))
        channel = (
            CH_MIC if _rms(mic[i0:i1]) >= _rms(system[i0:i1]) else CH_SYSTEM
        )
        cluster = _overlap_speaker(turns[channel], start, end) if turns[channel] else 0
        if cluster is None:
            cluster = 0
        out.append(
            LabeledSentence(start=start, end=end, text=text,
                            label=label_for(channel, cluster))
        )
    diarized = any(t is not None for t in turns.values())
    return out, diarized


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


def build_labeled_body(labeled: list) -> str:
    """Transcript body: one '[MM:SS] LABEL: text' line per sentence."""
    lines = []
    for s in labeled:
        m, sec = divmod(int(s.start), 60)
        lines.append(f"[{m:02d}:{sec:02d}] {s.label}: {s.text}")
    return "\n".join(lines)
