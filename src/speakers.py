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

import json
import logging
import os
import subprocess
import sys
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

# When the output folder is an Obsidian vault, attendee names in the document
# body render as [[wikilinks]] so every person becomes a linked note with
# backlinks from all their meetings. Set from [output] obsidian_links at
# startup; frontmatter stays plain names so parsers are unaffected.
OBSIDIAN_LINKS = False


def _fmt_names(names: list) -> str:
    if OBSIDIAN_LINKS:
        return ", ".join(f"[[{n}]]" for n in names)
    return ", ".join(names)

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


def worker_env() -> dict:
    """Environment for venv worker subprocesses. The frozen app's process
    carries py2app's PYTHONHOME/PYTHONPATH pointing INTO the bundle — a venv
    interpreter inheriting them loads the bundle's stdlib extensions and
    dies (the summarizer hit the bundle's _ssl.so this way). Strip them."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE")
    }


# ---------------------------------------------------------------------------
# WAV helpers
# ---------------------------------------------------------------------------

def repair_wav_header(path: Path) -> bool:
    """Fix a WAV whose RIFF/data size fields are still zero because the
    writer was killed before finalizing (a hard app kill takes the capture
    helper with it). The audio bytes themselves are intact — only 8 header
    bytes need patching, computed from the real file size. In-place,
    idempotent, refuses anything that isn't the exact canonical 44-byte
    layout our recorders write. Returns True if a repair was made."""
    try:
        size = path.stat().st_size
        if size <= 44:
            return False
        with open(path, "r+b") as f:
            h = f.read(44)
            if len(h) < 44 or h[:4] != b"RIFF" or h[36:40] != b"data":
                return False
            if int.from_bytes(h[40:44], "little") != 0:
                return False  # header already finalized
            f.seek(4)
            f.write((size - 8).to_bytes(4, "little"))
            f.seek(40)
            f.write((size - 44).to_bytes(4, "little"))
        log.warning(
            f"Repaired unfinalized WAV header (writer was killed mid-recording): {path.name}"
        )
        return True
    except Exception as e:
        log.debug(f"repair_wav_header({path}): {e}")
        return False


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

    repair_wav_header(path)
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


def load_channels(path: Path):
    """Load a recording for the labeling pipeline. Stereo split recordings
    return (mic, system, rate). MONO recordings (in-person mode, mic only)
    return (samples, empty, rate) — the whole meeting is the mic side, so
    diarization and live tags still work and labels come out as M1, M2, ...
    Returns None only when the file is unreadable."""
    import numpy as np

    loaded = load_split_channels(path)
    if loaded is not None:
        return loaded
    try:
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                return None
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception as e:
        log.warning(f"Could not read WAV {path}: {e}")
        return None
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, np.zeros(0, dtype=np.float32), rate


def downmix_to_mono_wav(src: Path, dst: Path) -> Optional[Path]:
    """
    Write a mono 16-bit WAV downmix of `src` to `dst` (saturating sum, same
    loudness as the pre-split mixed output). Mono sources are copied as-is.
    Used by recovery/batch paths whose STT backend expects mono.
    """
    import numpy as np

    repair_wav_header(src)
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


# Runs in a SEPARATE python process: sherpa-onnx's process() holds the GIL
# for its entire (minutes-long) run, which freezes the Qt main thread — the
# beach-ball-after-stop bug. A subprocess has its own GIL, so the app stays
# responsive. Keep this self-contained (stdlib + numpy + sherpa_onnx only).
# Preferred diarization backend (macOS): Senko — CoreML-accelerated
# 3D-Speaker pipeline with density-based clustering. Validated on real
# meetings against the sherpa pipeline below: matched the human-confirmed
# speaker count where sherpa over-split (5 vs 6-10 on the same audio), and
# runs ~30-60x faster (a 47-min channel in ~7 s on Apple Silicon).
_SENKO_WORKER = r"""
import json, sys
import numpy as np
import senko

wav = sys.argv[1]
tags = []
if len(sys.argv) > 2 and sys.argv[2].strip():
    try:
        tags = [(float(t), str(n)) for t, n in json.loads(sys.argv[2])]
    except Exception:
        tags = []

d = senko.Diarizer(quiet=True)

def emit(segments, centroids):
    print(json.dumps({"segments": segments, "centroids": centroids}))

def norm_cents(emb, labels):
    cents = {}
    lab = np.asarray(labels)
    for c in sorted(set(int(x) for x in lab)):
        m = np.mean(np.asarray(emb)[lab == c], axis=0)
        n = float(np.linalg.norm(m))
        if n > 0:
            m = m / n
        cents[str(c)] = [round(float(x), 6) for x in m]
    return cents

def plain():
    res = d.diarize(wav)
    if res is None:
        emit([], {})
        return
    ids, out = {}, []
    for s in res["merged_segments"]:
        spk = s["speaker"]
        if spk not in ids:
            ids[spk] = len(ids)
        out.append((float(s["start"]), float(s["end"]), ids[spk]))
    out.sort()
    cents = {}
    for spk, vec in (res.get("speaker_centroids") or {}).items():
        if spk in ids:
            v = np.asarray(vec, dtype=float).ravel()
            n = float(np.linalg.norm(v))
            if n > 0:
                v = v / n
            cents[str(ids[spk])] = [round(float(x), 6) for x in v]
    emit(out, cents)

def constrained():
    # Senko's own front half (VAD, fbank, CAM++ embeddings), then
    # tag-constrained spectral clustering instead of its UMAP+HDBSCAN:
    # live tags become must-link/cannot-link constraints and set a floor
    # on the speaker count (Turn-to-Diarize, arXiv 2109.11641).
    vad = d._perform_vad(wav)
    if not vad:
        emit([], {})
        return True
    subs = d._generate_subsegments(vad, None)
    if len(subs) < 4:
        return False
    feats, fps, offs, dim = d._extract_fbank_features(wav, subs)
    offs = [int(o) for o in offs]
    emb = np.asarray(d._generate_embeddings(feats, fps, offs, dim))
    n = len(subs)
    if emb.shape[0] != n:
        return False

    named = {}
    for t, name in tags:
        best, bd = None, None
        for i, (s0, e0) in enumerate(subs):
            if s0 - 1.0 <= t <= e0 + 2.5:
                dist = 0.0 if s0 <= t <= e0 else min(abs(t - s0), abs(t - e0))
                if bd is None or dist < bd:
                    best, bd = i, dist
        if best is not None:
            named.setdefault(best, name.strip().lower())

    M = np.zeros((n, n))
    items = list(named.items())
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            i, ni = items[a]
            j, nj = items[b]
            M[i, j] = M[j, i] = 1.0 if ni == nj else -1.0

    from spectralcluster import (
        ConstraintName,
        ConstraintOptions,
        ICASSP2018_REFINEMENT_SEQUENCE,
        RefinementOptions,
        SpectralClusterer,
        ThresholdType,
    )

    clusterer = SpectralClusterer(
        min_clusters=max(1, len(set(named.values()))),
        max_clusters=10,
        refinement_options=RefinementOptions(
            gaussian_blur_sigma=1,
            p_percentile=0.95,
            thresholding_soft_multiplier=0.01,
            thresholding_type=ThresholdType.RowMax,
            refinement_sequence=ICASSP2018_REFINEMENT_SEQUENCE,
        ),
        constraint_options=ConstraintOptions(
            constraint_name=ConstraintName.ConstraintPropagation,
            apply_before_refinement=True,
        ),
        max_spectral_size=2000,
    )
    labels = clusterer.predict(emb, constraint_matrix=M)

    uniq = sorted(set(int(x) for x in labels))
    remap = {u: i for i, u in enumerate(uniq)}
    lab = [remap[int(x)] for x in labels]
    segs = []
    for (s0, e0), l in zip(subs, lab):
        s0, e0 = float(max(0.0, s0)), float(max(0.0, e0))
        if segs and segs[-1][2] == l and s0 <= segs[-1][1] + 1.0:
            segs[-1][1] = max(segs[-1][1], e0)
            continue
        if segs and s0 < segs[-1][1]:
            mid = (segs[-1][1] + s0) / 2.0
            segs[-1][1] = mid
            s0 = mid
        segs.append([s0, e0, l])
    emit([(round(s, 3), round(e, 3), l) for s, e, l in segs], norm_cents(emb, lab))
    return True

done = False
if tags:
    try:
        done = constrained()
    except Exception as e:
        print("constrained clustering failed: %s" % e, file=sys.stderr)
        done = False
if not done:
    plain()
"""

_DIARIZE_WORKER = r"""
import json, sys, wave
import numpy as np
import sherpa_onnx

wav, seg, emb, thr = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
with wave.open(wav, "rb") as w:
    raw = w.readframes(w.getnframes())
samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg)),
    embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb),
    clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=thr),
    min_duration_on=0.3, min_duration_off=0.5)
sd = sherpa_onnx.OfflineSpeakerDiarization(config)
segs = sd.process(samples).sort_by_start_time()
print(json.dumps([[s.start, s.end, int(s.speaker)] for s in segs]))
"""


def _worker_python() -> Optional[str]:
    """Interpreter for the diarization subprocess. The Mac app already
    depends on the project venv at runtime (mlx & friends load from it), so
    it is the natural host. None means no usable interpreter (e.g. the
    frozen Windows .exe) — callers fall back to in-process diarization."""
    if os.environ.get("ALOE_SCRIBE_VENV"):
        p = Path(os.environ["ALOE_SCRIBE_VENV"]) / "bin" / "python3"
        if p.exists():
            return str(p)
    p = Path.home() / "aloe-scribe" / ".venv" / "bin" / "python3"
    if p.exists():
        return str(p)
    if not getattr(sys, "frozen", False):
        return sys.executable
    return None


class Diarizer:
    """
    Offline speaker diarization via sherpa-onnx. All failure modes (package
    missing, download blocked, model init error) degrade to unavailable —
    callers then fall back to channel-only labels.
    """

    # Calibrated on real meetings (see below) — configurable via
    # [transcriber] diarize_threshold. Lower = more, smaller speaker
    # clusters; higher = fewer, merged ones.
    DEFAULT_THRESHOLD = 1.0

    def __init__(self, cache_dir: Optional[Path] = None,
                 threshold: Optional[float] = None):
        self.cache_dir = cache_dir or default_cache_dir()
        self.threshold = float(threshold or self.DEFAULT_THRESHOLD)
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
                    num_clusters=-1, threshold=self.threshold
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

        WARNING: runs in-process and holds the GIL for the duration — only
        use where UI responsiveness doesn't matter (tests, CLI fallback).
        Prefer diarize_file() from the app.
        """
        if not self._ensure_ready():
            return None
        try:
            result = self._sd.process(samples).sort_by_start_time()
            return [(s.start, s.end, int(s.speaker)) for s in result]
        except Exception as e:
            log.warning(f"Diarization failed: {e}")
            return None

    def _senko_available(self, exe: str) -> bool:
        """Probe (once) whether the runtime venv has Senko."""
        if not hasattr(self, "_senko_ok"):
            try:
                self._senko_ok = (
                    subprocess.run(
                        [exe, "-c", "import senko"],
                        capture_output=True,
                        timeout=120,
                        env=worker_env(),
                    ).returncode
                    == 0
                )
            except Exception:
                self._senko_ok = False
            if not self._senko_ok:
                log.info("Senko not available — using sherpa-onnx diarization")
        return self._senko_ok

    def diarize_file(self, wav_path: Path, tags: Optional[list] = None) -> Optional[list]:
        """Diarize a mono 16 kHz WAV in a SUBPROCESS so the multi-minute
        model call cannot hold this process's GIL (which froze the app's
        UI). Prefers Senko (fast, better speaker counting), falls back to
        the sherpa pipeline, then to in-process sherpa. Same return shape
        as diarize().

        `tags` are the live (elapsed_s, name) speaker tags: they become
        must-link/cannot-link constraints inside Senko-path clustering, and
        the count of distinct tagged names floors the speaker count. After
        a Senko run, self.last_centroids maps cluster id to that voice's
        192-dim fingerprint (empty for the fallback paths)."""
        self.last_centroids = {}
        exe = _worker_python()
        if exe is not None and self._senko_available(exe):
            try:
                proc = subprocess.run(
                    [exe, "-c", _SENKO_WORKER, str(wav_path),
                     json.dumps([[float(t), str(n)] for t, n in (tags or [])])],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    env=worker_env(),
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    payload = json.loads(proc.stdout)
                    self.last_centroids = {
                        int(k): v
                        for k, v in (payload.get("centroids") or {}).items()
                    }
                    return [
                        (float(a), float(b), int(c))
                        for a, b, c in payload.get("segments", [])
                    ]
                log.warning(
                    f"Senko subprocess failed (rc={proc.returncode}): "
                    f"{(proc.stderr or '').strip()[-300:]} — falling back to sherpa"
                )
            except Exception as e:
                log.warning(f"Senko subprocess error: {e} — falling back to sherpa")
        if exe is not None:
            paths = self._model_paths()
            if paths is None:
                self._failed = True
                return None
            seg, emb = paths
            try:
                proc = subprocess.run(
                    [exe, "-c", _DIARIZE_WORKER, str(wav_path), str(seg),
                     str(emb), str(self.threshold)],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    env=worker_env(),
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return [
                        (float(a), float(b), int(c))
                        for a, b, c in json.loads(proc.stdout)
                    ]
                log.warning(
                    f"Diarization subprocess failed (rc={proc.returncode}): "
                    f"{(proc.stderr or '').strip()[-300:]}"
                )
            except Exception as e:
                log.warning(f"Diarization subprocess error: {e}")
            # fall through to in-process

        try:
            import numpy as np

            with wave.open(str(wav_path), "rb") as w:
                raw = w.readframes(w.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            return self.diarize(samples)
        except Exception as e:
            log.warning(f"Diarization fallback failed: {e}")
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


def dedupe_echo(mic_sentences: list, sys_sentences: list) -> tuple:
    """Drop duplicated speech that crossed channels, both directions.

    Two physical paths create duplicates, told apart by timing:

    1. Acoustic bleed, near-zero offset: the machine plays remote audio out
       its speakers next to its mic, so remote speech lands on both channels
       at the same instant. The system copy is authoritative — drop the mic
       copy. (A room voice cannot reach the system channel this way; system
       audio is a digital capture with no acoustic path.)
    2. Network echo, delayed seconds: room speech goes out through the call
       (a second laptop in the room with a live mic, or far-end speakers
       leaking into their mic) and comes BACK on the system channel — or the
       reverse. The round trip makes the echo the LATER copy, so keep the
       earlier one whichever channel it is on.

    Returns (mic_kept, sys_kept).
    """
    import difflib

    def _sim(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

    drop_mic, drop_sys = set(), set()
    for mi, m in enumerate(mic_sentences):
        dur = max(0.2, m[1] - m[0])
        for si, s in enumerate(sys_sentences):
            if si in drop_sys:
                continue
            overlap = min(m[1], s[1]) - max(m[0], s[0])
            if overlap / dur >= 0.5:
                if _sim(m[2], s[2]) > 0.65:
                    drop_mic.add(mi)
                    break
                continue
            # Delayed duplicate: only trusted on substantial sentences —
            # short acknowledgements ("Yeah.", "Okay.") legitimately repeat.
            gap = abs(m[0] - s[0])
            if gap <= 30.0 and len(m[2]) >= 25 and len(s[2]) >= 25:
                if _sim(m[2], s[2]) > 0.75:
                    if m[0] <= s[0]:
                        drop_sys.add(si)
                    else:
                        drop_mic.add(mi)
                        break
    mic_kept = [m for i, m in enumerate(mic_sentences) if i not in drop_mic]
    sys_kept = [s for i, s in enumerate(sys_sentences) if i not in drop_sys]
    return mic_kept, sys_kept


def _parse_sentences(sentences) -> list:
    parsed = []
    for s in sentences or []:
        f = _sentence_fields(s)
        if f:
            parsed.append(f)
    parsed.sort(key=lambda f: f[0])
    return parsed


class IncrementalChunker:
    """Process-as-you-go transcription: turns the recording into transcribed
    sentences WHILE the meeting runs, so stopping costs seconds instead of
    minutes.

    The live streaming loop feeds every batch of raw PCM here. Per channel,
    audio accumulates until ~CHUNK_S seconds are buffered, then the chunk is
    cut at the quietest moment near its end (so sentences aren't split
    mid-word), written to a temp WAV, and batch-transcribed; sentence
    timestamps are shifted by the chunk's absolute offset. Near-silent chunks
    are skipped without transcription. At stop, finalize() transcribes the
    remaining tail and returns {channel: [sentence dicts]} ready for
    assemble_labeled_transcript().

    Single-threaded by design: feed/maybe_process run on the streaming
    thread; finalize runs on the stop worker AFTER that thread has exited.
    """

    CHUNK_S = 90.0
    CUT_SEARCH_S = 15.0
    CUT_WIN_S = 0.3

    def __init__(self, transcribe_sentences, rate: int = 16000,
                 tmp_dir: Optional[Path] = None):
        import numpy as np  # noqa: F401 — fail fast if numpy is missing

        self._fn = transcribe_sentences
        self._rate = rate
        self._tmp_dir = Path(tmp_dir) if tmp_dir else default_cache_dir().parent
        self._buffers = {CH_MIC: [], CH_SYSTEM: []}
        self._offset_s = {CH_MIC: 0.0, CH_SYSTEM: 0.0}
        self.sentences = {CH_MIC: [], CH_SYSTEM: []}
        self.chunks_done = 0

    def feed(self, raw: bytes, channels: int):
        """Interleaved int16 PCM from the growing WAV (stereo split only)."""
        import numpy as np

        if not raw or channels not in (1, 2):
            return
        arr = np.frombuffer(raw, dtype=np.int16)
        if channels == 1:
            # In-person mono: everything is the mic side.
            self._buffers[CH_MIC].append(arr.astype(np.float32) / 32768.0)
            return
        arr = arr[: (len(arr) // 2) * 2].reshape(-1, 2)
        self._buffers[CH_MIC].append(
            arr[:, CH_MIC].astype(np.float32) / 32768.0
        )
        self._buffers[CH_SYSTEM].append(
            arr[:, CH_SYSTEM].astype(np.float32) / 32768.0
        )

    def _buffered_s(self, ch) -> float:
        return sum(len(b) for b in self._buffers[ch]) / self._rate

    def maybe_process(self) -> bool:
        """Transcribe at most ONE due chunk (keeps live-preview stalls short).
        Returns True if a chunk was processed."""
        for ch in (CH_MIC, CH_SYSTEM):
            if self._buffered_s(ch) >= self.CHUNK_S:
                self._process(ch, final=False)
                return True
        return False

    def _cut_index(self, samples) -> int:
        """Quietest CUT_WIN_S window within the last CUT_SEARCH_S of the
        chunk — cutting there avoids splitting a sentence mid-word."""
        import numpy as np

        n = int(self.CHUNK_S * self._rate)
        lo = max(0, n - int(self.CUT_SEARCH_S * self._rate))
        win = int(self.CUT_WIN_S * self._rate)
        step = int(0.1 * self._rate)
        best_i, best_rms = n, None
        for i in range(lo, n - win, step):
            seg = samples[i:i + win]
            rms = float(np.sqrt((seg * seg).mean()))
            if best_rms is None or rms < best_rms:
                best_rms, best_i = rms, i + win // 2
        return best_i

    def _process(self, ch: int, final: bool):
        import numpy as np

        if not self._buffers[ch]:
            return
        samples = np.concatenate(self._buffers[ch])
        cut = len(samples) if final else self._cut_index(samples)
        chunk, rest = samples[:cut], samples[cut:]
        self._buffers[ch] = [rest] if len(rest) else []
        offset = self._offset_s[ch]
        self._offset_s[ch] += len(chunk) / self._rate
        if len(chunk) < self._rate:  # < 1 s — nothing worth transcribing
            return
        if activity_fraction(chunk, self._rate) < 0.004:
            return  # silent stretch — skip the model call entirely
        tmp = self._tmp_dir / f".aloe-chunk-{ch}-{int(offset)}.wav"
        try:
            write_channel_wav(chunk, self._rate, tmp)
            sents = self._fn(tmp) or []
            for s in sents:
                try:
                    s["start"] = float(s.get("start", 0) or 0) + offset
                    s["end"] = float(s.get("end", 0) or 0) + offset
                except Exception:
                    continue
            self.sentences[ch].extend(sents)
            self.chunks_done += 1
        except Exception as e:
            log.warning(f"Incremental chunk failed (ch{ch} @{int(offset)}s): {e}")
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    def finalize(self) -> dict:
        """Transcribe the remaining tails; returns {channel: sentences}."""
        for ch in (CH_MIC, CH_SYSTEM):
            self._process(ch, final=True)
        return {ch: list(s) for ch, s in self.sentences.items()}


def build_labeled_transcript(
    wav_path: Path,
    transcribe_sentences,
    diarizer: Optional[Diarizer],
    title: str,
    when,
    source: str,
    progress=None,
    tags: Optional[list] = None,
    notes: Optional[list] = None,
    attendees: Optional[list] = None,
) -> Optional[str]:
    """
    Full speaker-labeled transcript from a split (stereo) recording.

    Each channel is transcribed SEPARATELY — the channels are clean single-
    party audio, so overlapping speech (two people talking at once) is fully
    recovered instead of fighting for one mono downmix, and the mic/remote
    attribution is structural rather than an energy guess. Near-silent
    channels are skipped. Voices within a channel are split by diarization.

    `transcribe_sentences`: callable(Path) -> list of {'start','end','text'}
    (the backend's batch API). `progress`: optional callable(str) receiving
    human-readable stage updates for the UI.

    `tags`: [(seconds_from_start, name), ...] captured live in the meeting
    panel ("Brandon is talking right now"). Clusters are named by the tags
    that land inside their speech, which beats any post-hoc guessing: the
    person doing the naming could HEAR the speaker at tag time. Two clusters
    tagged with the same name merge automatically, healing diarization
    over-splits. Untagged clusters keep anonymous M/R labels.

    `notes`: [(seconds_from_start, text), ...] typed during the meeting —
    appended as a timestamped Notes section for the summary agent.

    Returns the markdown string, or None when the WAV is not a split
    recording or nothing was transcribed — callers fall back to the plain
    downmix path.

    Speaker identification (CPU, onnxruntime) runs on a background thread
    CONCURRENTLY with transcription (GPU on the Mac), so the wall-clock cost
    is roughly max() of the two instead of their sum.
    """
    import threading

    from frontmatter import build_frontmatter

    def note(msg: str):
        if progress is None:
            return
        try:
            progress(msg)
        except Exception:
            pass

    note("Analyzing audio channels…")
    loaded = load_channels(wav_path)
    if loaded is None:
        return None
    mic, system, rate = loaded

    chan_samples = {CH_MIC: mic, CH_SYSTEM: system}
    chan_names = {CH_MIC: "your mic side", CH_SYSTEM: "the remote audio"}
    active = [
        ch for ch in (CH_MIC, CH_SYSTEM)
        if activity_fraction(chan_samples[ch], rate) >= 0.005
    ]
    for ch in (CH_MIC, CH_SYSTEM):
        if ch not in active:
            log.info(
                f"Channel {ch} ({'mic' if ch == CH_MIC else 'system'}) "
                "is silent — skipping its transcription"
            )

    # Write the per-channel mono WAVs up front: the STT loop reads them, and
    # so does the diarization SUBPROCESS (out-of-process because sherpa-onnx
    # holds the GIL for its whole run — in a thread it froze the app UI).
    tmp_wavs = {
        ch: wav_path.with_name(f".{wav_path.stem}.ch{ch}.wav") for ch in active
    }
    turns = {CH_MIC: None, CH_SYSTEM: None}
    per_channel: dict = {CH_MIC: [], CH_SYSTEM: []}
    try:
        for ch in active:
            write_channel_wav(chan_samples[ch], rate, tmp_wavs[ch])

        chan_centroids: dict = {}

        def _diarize_all():
            for ch in active:
                turns[ch] = diarizer.diarize_file(tmp_wavs[ch], tags=tags)
                chan_centroids[ch] = dict(
                    getattr(diarizer, "last_centroids", {}) or {}
                )

        diar_thread = None
        if diarizer is not None and active:
            diar_thread = threading.Thread(target=_diarize_all, daemon=True)
            diar_thread.start()

        for i, channel in enumerate(active):
            note(f"Transcribing {chan_names[channel]} ({i + 1}/{len(active)})…")
            sentences = transcribe_sentences(tmp_wavs[channel])
            per_channel[channel] = _parse_sentences(sentences)

        per_channel[CH_MIC], per_channel[CH_SYSTEM] = dedupe_echo(
            per_channel[CH_MIC], per_channel[CH_SYSTEM]
        )
        if not per_channel[CH_MIC] and not per_channel[CH_SYSTEM]:
            return None

        if diar_thread is not None:
            note("Identifying speakers…")
            diar_thread.join()
    finally:
        for t in tmp_wavs.values():
            try:
                t.unlink()
            except Exception:
                pass

    return _label_and_render(
        per_channel, turns, tags, notes, attendees, title, when, source,
        centroids=chan_centroids,
    )


def _label_and_render(
    per_channel: dict,
    turns: dict,
    tags: Optional[list],
    notes: Optional[list],
    attendees: Optional[list],
    title: str,
    when,
    source: str,
    centroids: Optional[dict] = None,
) -> Optional[str]:
    """Shared final stage: cluster-tag every sentence, name clusters from the
    live tags, and render frontmatter + body + notes. Used by both the batch
    path and the incremental (process-as-you-go) path."""
    from frontmatter import build_frontmatter

    # Channels whose transcription came back empty don't need their turns.
    for ch in (CH_MIC, CH_SYSTEM):
        if not per_channel[ch]:
            turns[ch] = None

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

    # Live speaker tags → cluster names. A tag votes for the cluster whose
    # sentence contains its timestamp (small tolerance: people tag a beat
    # after someone starts talking, and turn boundaries wobble).
    cluster_names = _names_from_tags(tagged, tags)

    label_map: dict = {}
    counts = {CH_MIC: 0, CH_SYSTEM: 0}
    for start, end, text, channel, cluster in tagged:
        key = (channel, cluster)
        if key not in label_map:
            name = cluster_names.get(key)
            if name:
                label_map[key] = name
            else:
                counts[channel] += 1
                label_map[key] = f"{_LABEL_PREFIX[channel]}{counts[channel]}"

    # Elimination: when the roster has exactly one attendee left unassigned
    # and exactly one speaker cluster is still anonymous, they must be the
    # same person. Strictly one-to-one — with two of either it would be a
    # guess (diarization over-splits often), so anything else stays anonymous.
    roster_names = [_sanitize_name(str(a)) for a in (attendees or [])]
    roster_names = [a for a in roster_names if a]
    if roster_names:
        named_lower = {n.lower() for n in cluster_names.values()}
        unassigned = [a for a in roster_names if a.lower() not in named_lower]
        anon_keys = [
            k for k, lbl in label_map.items()
            if k not in cluster_names
        ]
        if len(unassigned) == 1 and len(anon_keys) == 1:
            label_map[anon_keys[0]] = unassigned[0]

    # Named clusters carry a voice fingerprint worth keeping: enrollment
    # rides the naming gestures, so a tagged colleague can be recognized in
    # future meetings without tagging again (see voice_profiles).
    if centroids:
        try:
            import re as _re

            import voice_profiles

            anon = _re.compile(r"^(M|R)\d+$")
            secs: dict = {}
            for start, end, _text, channel, cluster in tagged:
                key = (channel, cluster)
                secs[key] = secs.get(key, 0.0) + max(0.0, end - start)
            for key, lbl in label_map.items():
                if anon.match(lbl):
                    continue
                ch, cluster = key
                vec = (centroids.get(ch) or {}).get(cluster)
                if not vec:
                    continue
                voice_profiles.record(
                    lbl,
                    "mic" if ch == CH_MIC else "system",
                    vec,
                    secs.get(key, 0.0),
                    "tag" if key in cluster_names else "elimination",
                )
        except Exception as e:
            log.warning(f"Voice profile update skipped: {e}")

    labeled = [
        LabeledSentence(
            start=start, end=end, text=text, label=label_map[(channel, cluster)]
        )
        for start, end, text, channel, cluster in tagged
    ]

    diarized = any(t is not None for t in turns.values())
    return render_document(title, when, source, labeled, notes, attendees, diarized)


def render_document(
    title: str,
    when,
    source: str,
    labeled: list,
    notes: Optional[list],
    attendees: Optional[list],
    diarized: bool,
) -> str:
    """The transcript document, structured for retrieval agents: a real title
    and plain-text metadata lead the body, the user's notes come next (their
    highest-signal content), and the raw timestamped dialogue is demoted to a
    Transcript section at the end. A summarizer later inserts Summary and
    Action items sections after the metadata (see summarizer.insert_summary).
    Frontmatter stays for tools that read structured keys."""
    from frontmatter import build_frontmatter

    duration = max((s.end for s in labeled), default=0.0)
    fm = build_frontmatter(
        title,
        when,
        duration,
        source=source,
        extras=speaker_frontmatter_extras(labeled, diarized, attendees),
    )

    when_local = when.astimezone() if hasattr(when, "astimezone") else when
    meta = [
        f"# {title}",
        "",
        f"{when_local.strftime('%A, %B')} {when_local.day}, {when_local.year} "
        f"at {when_local.strftime('%H:%M')} · {max(1, round(duration / 60))} min",
    ]
    roster = [_sanitize_name(str(a)) for a in (attendees or [])]
    roster = [a for a in roster if a]
    if roster:
        meta.append(f"Attendees: {_fmt_names(roster)}")
    seen: list = []
    for s in labeled:
        if s.label not in seen:
            seen.append(s.label)
    meta.append(f"Speakers heard: {', '.join(seen)}")

    notes_md = build_notes_section(notes)
    body = build_labeled_body(labeled)
    return (
        fm + "\n\n" + "\n".join(meta) + notes_md
        + "\n\n## Transcript\n\n" + body + "\n"
    )


def assemble_labeled_transcript(
    wav_path: Path,
    per_channel_sentences: dict,
    diarizer: Optional[Diarizer],
    title: str,
    when,
    source: str,
    progress=None,
    tags: Optional[list] = None,
    notes: Optional[list] = None,
    attendees: Optional[list] = None,
) -> Optional[str]:
    """Final transcript from ALREADY-TRANSCRIBED sentences (the
    process-as-you-go path): the STT work happened in chunks during the
    meeting, so this only runs diarization over the finished channels and
    renders. With Senko that is seconds, which is what makes stop-to-
    transcript nearly instant."""

    def note(msg: str):
        if progress is None:
            return
        try:
            progress(msg)
        except Exception:
            pass

    loaded = load_channels(wav_path)
    if loaded is None:
        return None
    mic, system, rate = loaded
    chan_samples = {CH_MIC: mic, CH_SYSTEM: system}

    per_channel = {
        CH_MIC: _parse_sentences(per_channel_sentences.get(CH_MIC) or []),
        CH_SYSTEM: _parse_sentences(per_channel_sentences.get(CH_SYSTEM) or []),
    }
    per_channel[CH_MIC], per_channel[CH_SYSTEM] = dedupe_echo(
        per_channel[CH_MIC], per_channel[CH_SYSTEM]
    )
    if not per_channel[CH_MIC] and not per_channel[CH_SYSTEM]:
        return None

    turns = {CH_MIC: None, CH_SYSTEM: None}
    active = [ch for ch in (CH_MIC, CH_SYSTEM) if per_channel[ch]]
    tmp_wavs = {
        ch: wav_path.with_name(f".{wav_path.stem}.ch{ch}.wav") for ch in active
    }
    try:
        chan_centroids: dict = {}
        if diarizer is not None:
            note("Identifying speakers…")
            for ch in active:
                write_channel_wav(chan_samples[ch], rate, tmp_wavs[ch])
                turns[ch] = diarizer.diarize_file(tmp_wavs[ch], tags=tags)
                chan_centroids[ch] = dict(
                    getattr(diarizer, "last_centroids", {}) or {}
                )
    finally:
        for t in tmp_wavs.values():
            try:
                t.unlink()
            except Exception:
                pass

    note("Building the transcript…")
    return _label_and_render(
        per_channel, turns, tags, notes, attendees, title, when, source,
        centroids=chan_centroids,
    )


def _names_from_tags(tagged: list, tags: Optional[list]) -> dict:
    """Map (channel, cluster) → human name from live tags.

    Each tag (t, name) votes for the cluster of the sentence spanning t
    (with -1/+4 s tolerance — tags trail speech onset). Majority per cluster
    wins; the same name winning several clusters merges them in the output.
    """
    if not tags:
        return {}
    votes: dict = {}
    for raw_t, raw_name in tags:
        try:
            t = float(raw_t)
        except (TypeError, ValueError):
            continue
        name = _sanitize_name(str(raw_name or ""))
        if not name:
            continue
        best_key, best_dist = None, None
        for start, end, _text, channel, cluster in tagged:
            if start - 1.0 <= t <= end + 4.0:
                dist = 0.0 if start <= t <= end else min(
                    abs(t - start), abs(t - end)
                )
                if best_dist is None or dist < best_dist:
                    best_key, best_dist = (channel, cluster), dist
        if best_key is not None:
            votes.setdefault(best_key, {})
            votes[best_key][name] = votes[best_key].get(name, 0) + 1
    # One canonical casing per person ACROSS clusters (first-typed wins), so
    # "brandon" and "Brandon" tags produce one merged speaker, not two.
    canonical: dict = {}
    for _t, raw_name in tags:
        name = _sanitize_name(str(raw_name or ""))
        if name:
            canonical.setdefault(name.lower(), name)

    out = {}
    for key, names in votes.items():
        merged: dict = {}
        for n, c in names.items():
            k = n.lower()
            merged[k] = merged.get(k, 0) + c
        winner = max(merged.items(), key=lambda kv: kv[1])[0]
        out[key] = canonical[winner]
    return out


def transcript_date(md_text: str):
    """The frontmatter `date` of a transcript, or None."""
    import re
    from datetime import datetime

    m = re.search(r"^date: (.+)$", md_text, flags=re.M)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).strip())
    except ValueError:
        return None


def merge_transcripts(parts: list) -> Optional[str]:
    """Merge transcripts of ONE meeting that got split (app quit mid-call,
    crash and restart) into a single coherent transcript.

    `parts`: list of markdown texts. They are ordered by their frontmatter
    `date`, later parts' timestamps are shifted by the real wall-clock gap
    (so the merged [MM:SS] times reflect the actual meeting timeline,
    including the gap where nothing recorded), anonymous labels are
    renumbered so part 2's R1 does not collide with part 1's different R1,
    named speakers with the same name merge, and attendees and notes
    combine. Returns None if fewer than two parseable parts."""
    import re
    from datetime import datetime

    from frontmatter import build_frontmatter

    parsed = []
    for text in parts:
        m_date = re.search(r"^date: (.+)$", text, flags=re.M)
        m_end = re.search(r"^end: (.+)$", text, flags=re.M)
        m_title = re.search(r'^title: "(.*)"$', text, flags=re.M)
        if not m_date:
            continue
        try:
            date = datetime.fromisoformat(m_date.group(1).strip())
            end = (
                datetime.fromisoformat(m_end.group(1).strip())
                if m_end
                else date
            )
        except ValueError:
            continue
        m_att = re.search(r"^attendees: \[([^\]]*)\]$", text, flags=re.M)
        attendees = (
            [a.strip() for a in m_att.group(1).split(",") if a.strip()]
            if m_att
            else []
        )
        m_src = re.search(r"^source: (.+)$", text, flags=re.M)
        # Notes live in their own section (which may sit BEFORE the
        # Transcript section in the current layout) — bound it by the next
        # "## " header. Transcript lines carry a "Label:" and never match
        # inside notes, so they can be collected from the whole document.
        m_notes = re.search(r"^## Notes\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
        notes_part = m_notes.group(1) if m_notes else ""
        lines = []
        for line in text.splitlines():
            lm = re.match(_LINE_RE, line)
            if lm and not lm.group(3).startswith("#"):
                sec = int(lm.group(1)) * 60 + int(lm.group(2))
                lines.append((float(sec), lm.group(3), lm.group(4)))
        parsed.append({
            "date": date,
            "end": end,
            "title": m_title.group(1) if m_title else "Recording",
            "source": (m_src.group(1).strip() if m_src else "aloe-scribe"),
            "attendees": attendees,
            "lines": lines,
            "notes": parse_notes_log(notes_part),
        })

    if len(parsed) < 2:
        return None
    parsed.sort(key=lambda p: p["date"])
    base = parsed[0]

    anon = re.compile(r"^([MR])(\d+)$")
    counts = {"M": 0, "R": 0}
    all_lines = []
    all_notes = []
    attendees: list = []
    for part in parsed:
        offset = (part["date"] - base["date"]).total_seconds()
        # Renumber this part's anonymous labels past everything used so far;
        # named speakers pass through and merge naturally.
        relabel = {}
        for _sec, label, _text in part["lines"]:
            am = anon.match(label)
            if am and label not in relabel:
                counts[am.group(1)] += 1
                relabel[label] = f"{am.group(1)}{counts[am.group(1)]}"
        for sec, label, text in part["lines"]:
            all_lines.append(
                LabeledSentence(
                    start=sec + offset,
                    end=sec + offset,
                    text=text,
                    label=relabel.get(label, label),
                )
            )
        for t, note_text in part["notes"]:
            all_notes.append(
                (t + offset if t is not None else None, note_text)
            )
        for a in part["attendees"]:
            if a.lower() not in [x.lower() for x in attendees]:
                attendees.append(a)

    if not all_lines:
        return None
    all_lines.sort(key=lambda s: s.start)
    return render_document(
        base["title"],
        base["date"],
        base["source"],
        all_lines,
        all_notes,
        attendees,
        True,
    )


def parse_notes_log(text: str) -> list:
    """Notes-window log → [(seconds or None, text)] entries.

    Log lines look like '[MM:SS] note text'. Tag confirmations
    ('[MM:SS] ▸ Name is speaking') are speaker data already carried by the
    tags themselves, so they are skipped here. Untimestamped lines (the user
    edited freely) are kept without a stamp."""
    import re

    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[(\d+):(\d\d)\]\s*(.*)$", line)
        if m:
            t = int(m.group(1)) * 60 + int(m.group(2))
            body = m.group(3).strip()
        else:
            t, body = None, line
        if not body or body.startswith("▸"):
            continue
        out.append((t, body))
    return out


def build_notes_section(notes: Optional[list]) -> str:
    """Timestamped Notes section appended to the transcript ('' if none).
    Notes are the user's own words — the summary agent should treat them as
    higher-signal than the transcript itself."""
    if not notes:
        return ""
    lines = ["", "", "## Notes", ""]
    for raw_t, raw_text in notes:
        text = str(raw_text or "").strip()
        if not text:
            continue
        try:
            m, s = divmod(int(float(raw_t)), 60)
            stamp = f"[{m:02d}:{s:02d}] "
        except (TypeError, ValueError):
            stamp = ""
        lines.append(f"{stamp}{text}")
    if len(lines) == 4:
        return ""
    return "\n".join(lines)


def speaker_frontmatter_extras(
    labeled: list, diarized: bool, attendees: Optional[list] = None
) -> list:
    """Frontmatter lines describing the labeling scheme for the summary agent.
    `attendees` is the user-entered roster — everyone ON the call, including
    people who never spoke, which the transcript alone can never reveal."""
    seen = []
    for s in labeled:
        if s.label not in seen:
            seen.append(s.label)
    scheme = (
        "M speakers were heard on the local microphone. That is the in-room "
        "side and it may be one or several people, not necessarily the "
        "machine's owner. R speakers were heard on system audio, meaning the "
        "remote participants. Match labels to names using the calendar event "
        "and conversational context."
    )
    lines = [
        "tags: [meeting]",
        f"speakers: [{', '.join(seen)}]",
        f'speaker_key: "{scheme}"',
    ]
    roster = [_sanitize_name(str(a)) for a in (attendees or [])]
    roster = [a for a in roster if a]
    if roster:
        lines.append(f"attendees: [{', '.join(roster)}]")
    if not diarized:
        lines.append(
            'speaker_note: "Diarization was unavailable for this recording. Each '
            'label is a channel and may lump several voices together."'
        )
    return lines


_LINE_RE = r"^\[(\d+):(\d\d)\] ([A-Za-z][\w .'-]*?): (.*)$"


def speaker_quotes(md_text: str) -> list:
    """From a labeled transcript, one entry per speaker in order of first
    appearance: (label, quotes, line_count). `quotes` is up to three of the
    speaker's lines — their first, their longest, and their last, in
    chronological order — because a single line is often not enough to
    recognize who was talking. Used by the "who was speaking?" prompt."""
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

    def trim(q: str) -> str:
        return q if len(q) <= 110 else q[:107].rsplit(" ", 1)[0] + "…"

    out = []
    for label in order:
        lines = by_label[label]
        longest = max(lines, key=len)
        picks = []
        for q in (lines[0], longest, lines[-1]):  # chronological already
            if q not in picks:
                picks.append(q)
        out.append((label, [trim(q) for q in picks], len(lines)))
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

    # Merge case-insensitively — and against names ALREADY IN THE DOCUMENT,
    # not just within this mapping. Renames arrive one chip at a time, so
    # typing "roi leibovich" when the doc has "Roi Leibovich" must merge into
    # the existing speaker, not mint a case-variant twin. Existing casing
    # wins, then first-typed casing.
    canonical: dict = {}
    for line in md_text.splitlines():
        m = re.match(_LINE_RE, line)
        if m:
            canonical.setdefault(m.group(3).lower(), m.group(3))
    for label in sorted(mapping):
        key = mapping[label].lower()
        canonical.setdefault(key, mapping[label])
    mapping = {label: canonical[name.lower()] for label, name in mapping.items()}

    # Channel notes come from the mapping itself — the body's first-seen
    # walk misses a label merged into an already-present name.
    channel_notes = [
        f"{name}={label}"
        for label, name in sorted(mapping.items())
        if label != name
    ]

    out_lines = []
    final_order = []
    seen = set()
    for line in md_text.splitlines():
        m = re.match(_LINE_RE, line)
        if m:
            label = m.group(3)
            name = mapping.get(label, label)
            if name not in seen:
                seen.add(name)
                final_order.append(name)
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
    # Renames arrive one at a time: merge this rename's channel notes with
    # any existing speaker_channels line instead of stacking duplicates.
    existing = re.search(r'^speaker_channels: "([^"]*)"$', text, flags=re.M)
    if existing:
        prior = [n.strip() for n in existing.group(1).split(";") if n.strip()]
        new_pairs = {n.lower() for n in channel_notes}
        channel_notes = [
            n for n in prior if n.lower() not in new_pairs
        ] + channel_notes
        text = re.sub(r'^speaker_channels: "[^"]*"\n?', "", text, flags=re.M)
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
