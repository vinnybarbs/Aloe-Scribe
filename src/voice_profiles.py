"""
voice_profiles.py — persistent, on-device voice fingerprints.

Every diarized cluster comes with a 192-dim CAM++ centroid (its voice
fingerprint). When a cluster ends up NAMED — a live tag, or elimination
against the roster — that fingerprint is worth keeping: next meeting, the
same voice can be recognized without any tagging.

Design rules (from the Aug 2026 research pass):
  - Session centroids, never one global mean: each entry keeps the channel
    it came from (a voice through Teams processing is measurably different
    from the same voice in the room), the date, and how it was confirmed.
  - Everything stays in one user-readable JSON on this machine. No audio is
    stored, nothing is ever transmitted, delete the file and it is gone.
  - Guard the update path: a mislabeled cluster must not poison a profile,
    so implicit entries need minimum evidence and rough similarity to what
    is already stored, and tag-confirmed entries are preferred when capping.
  - The file is versioned by embedding model, so a future model swap
    invalidates cleanly instead of mixing incompatible spaces.
"""

import json
import logging
import math
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_ID = "senko-campplus-192"

# A profile entry needs real evidence behind it: ~30 s of speech saturates
# speaker-verification enrollment, below ~20 s the centroid is noisy.
MIN_SECONDS = 25.0

# Per person per channel; oldest non-tag entries are dropped first.
MAX_ENTRIES_PER_CHANNEL = 10

# An implicit (non-tag) entry that looks nothing like the person's existing
# same-channel fingerprints is more likely a mislabel than a new voice.
MISLABEL_FLOOR = 0.30


def profile_path() -> Path:
    override = os.environ.get("ALOE_PROFILE_DIR")
    root = (
        Path(override).expanduser()
        if override
        else Path.home() / ".local" / "share" / "aloe-scribe"
    )
    return root / "speakers.json"


def _empty() -> dict:
    return {"version": 1, "embedding_model": MODEL_ID, "people": {}}


def load() -> dict:
    try:
        data = json.loads(profile_path().read_text(encoding="utf-8"))
        if data.get("embedding_model") != MODEL_ID:
            log.info("Voice profile store is for a different model — starting fresh")
            return _empty()
        if not isinstance(data.get("people"), dict):
            return _empty()
        return data
    except FileNotFoundError:
        return _empty()
    except Exception as e:
        log.warning(f"Voice profile store unreadable ({e}) — starting fresh")
        return _empty()


def _save(data: dict):
    p = profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(p)


def _cos(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    return num / (da * db) if da > 0 and db > 0 else 0.0


def record(name: str, channel: str, vector, seconds: float, how: str) -> bool:
    """Store one session centroid for `name`. channel is "mic" or "system";
    how is "tag" (live tag confirmed the cluster) or "elimination".
    Returns True when the entry was stored."""
    name = (name or "").strip()
    if not name or not vector:
        return False
    if seconds < MIN_SECONDS:
        log.debug(
            f"Voice profile skip: {name} had {seconds:.0f}s of speech "
            f"(need {MIN_SECONDS:.0f})"
        )
        return False
    vec = [round(float(x), 6) for x in vector]

    data = load()
    key = name.lower()
    person = data["people"].setdefault(
        key,
        {"name": name, "created": time.strftime("%Y-%m-%d"), "entries": []},
    )
    same_channel = [e for e in person["entries"] if e.get("channel") == channel]
    if same_channel and how != "tag":
        best = max(_cos(vec, e["v"]) for e in same_channel)
        if best < MISLABEL_FLOOR:
            log.info(
                f"Voice profile skip: implicit entry for {name} does not "
                f"resemble their stored voice (cos {best:.2f})"
            )
            return False
    person["entries"].append(
        {
            "v": vec,
            "channel": channel,
            "date": time.strftime("%Y-%m-%d"),
            "seconds": round(float(seconds), 1),
            "how": how,
        }
    )
    same_channel = [e for e in person["entries"] if e.get("channel") == channel]
    while len(same_channel) > MAX_ENTRIES_PER_CHANNEL:
        drop = next((e for e in same_channel if e.get("how") != "tag"), same_channel[0])
        person["entries"].remove(drop)
        same_channel.remove(drop)
    person["last_heard"] = time.strftime("%Y-%m-%d")
    _save(data)
    log.info(f"Voice profile updated: {name} ({channel}, {seconds:.0f}s, {how})")
    return True


def match(vector, channel: str) -> list:
    """Score a cluster centroid against every stored profile.

    Returns [(display_name, score, same_channel)] best first, where score is
    the max cosine over that person's stored entries, preferring same-channel
    entries when any exist. Callers decide thresholds; this just scores."""
    if not vector:
        return []
    data = load()
    out = []
    for person in data["people"].values():
        entries = person.get("entries") or []
        same = [e for e in entries if e.get("channel") == channel]
        pool = same or entries
        if not pool:
            continue
        score = max(_cos(vector, e["v"]) for e in pool)
        out.append((person.get("name", "?"), score, bool(same)))
    out.sort(key=lambda t: t[1], reverse=True)
    return out
