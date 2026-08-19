"""
summarizer.py — local executive summary for finished transcripts.

After a transcript lands, a small local LLM (Qwen 3.5 4B, 4-bit MLX) writes a
Summary and Action items block into the top of the .md. Nothing leaves the
machine: the model runs on-device via mlx-lm, the same runtime Parakeet uses.

Runs in a SUBPROCESS (own GIL, own Metal context) so a running or upcoming
recording never contends with it — the same isolation pattern as the Senko
diarizer and the chunk transcriber. Model weights resolve like Parakeet's:
a local folder fetched from this repo's GitHub Releases first, the Hugging
Face id as a dev fallback.

The prompt is steered by the user's own meeting artifacts — their notes,
attendee roster, and speaker names are all in the document the model reads —
so the summary reflects what the user flagged as important, not a generic
compression of the dialogue.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "mlx-community/Qwen3.5-4B-MLX-4bit"
LOCAL_MODEL_DIRNAME = "qwen3.5-4b-mlx-4bit"

# Keep the model's input bounded: a 4-bit 4B holds long meetings fine, but a
# runaway multi-hour document should not stall the pipeline. Middle-truncate
# beyond this many characters (~2.5 h of speech).
MAX_INPUT_CHARS = 90_000

_PROMPT = """You are writing the executive summary block for a meeting transcript. Rules: plain business English. Never use em dashes, semicolons, arrows, or the words leverage, utilize, robust, seamless, streamline, holistic, foster, delve. Lead with the point. Use the speaker names that appear in the transcript. The Notes section, when present, is the user's own notes and outranks the dialogue for what mattered.

Produce exactly two sections and nothing else:
## Summary
3 to 6 short bullets covering what the meeting was about and what was decided or learned.
## Action items
One line per real commitment from the conversation, each starting with the owner's name. If none, write exactly: None captured.

Document:
"""

_WORKER = r"""
import sys
model_path = sys.argv[1]
prompt_text = sys.stdin.read()
from mlx_lm import load, generate
model, tokenizer = load(model_path)
messages = [{"role": "user", "content": prompt_text}]
text = tokenizer.apply_chat_template(
    messages, add_generation_prompt=True, enable_thinking=False
)
out = generate(model, tokenizer, prompt=text, max_tokens=600, verbose=False)
sys.stdout.write(out)
"""


def resolve_model(configured: str = "") -> Optional[str]:
    """The model to load: an explicit configured path, the repo-local folder
    the installer fetches, or the Hugging Face id as a dev fallback (which
    only works where HF is reachable and not forced offline)."""
    if configured:
        p = Path(configured).expanduser()
        return str(p) if p.is_dir() else configured
    here = Path(__file__).resolve().parent.parent / "models" / LOCAL_MODEL_DIRNAME
    if here.is_dir():
        return str(here)
    return DEFAULT_MODEL_ID


def _worker_python() -> Optional[str]:
    """The runtime venv's interpreter (mlx-lm lives there, not in the frozen
    app)."""
    for candidate in (
        Path.home() / "aloe-scribe" / ".venv" / "bin" / "python3",
        Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python3",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def generate_summary(md_text: str, model: str) -> Optional[str]:
    """Run the model over the document; returns the '## Summary ...' block or
    None on any failure. Subprocess-isolated, bounded at five minutes."""
    exe = _worker_python()
    if exe is None:
        log.info("No runtime venv found. Summarizer disabled.")
        return None
    doc = md_text
    if len(doc) > MAX_INPUT_CHARS:
        half = MAX_INPUT_CHARS // 2
        doc = (
            doc[:half]
            + "\n\n[transcript truncated for length]\n\n"
            + doc[-half:]
        )
    try:
        proc = subprocess.run(
            [exe, "-c", _WORKER, model],
            input=_PROMPT + doc,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as e:
        log.warning(f"Summarizer subprocess error: {e}")
        return None
    if proc.returncode != 0:
        log.warning(
            f"Summarizer failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()[-300:]}"
        )
        return None
    out = proc.stdout
    # Strip any preamble the model produced before the block.
    idx = out.find("## Summary")
    if idx < 0:
        log.warning("Summarizer output had no Summary section. Discarded.")
        return None
    block = out[idx:].strip()
    # Cut anything trailing after the Action items section.
    m = re.search(r"^## (?!Summary|Action items)", block, flags=re.M)
    if m:
        block = block[: m.start()].strip()
    return block


def insert_summary(md_text: str, block: str) -> str:
    """Place the Summary/Action items block after the document's metadata,
    before the first existing section. Idempotent: an earlier summary block
    is replaced, so re-summarizing never stacks duplicates."""
    # Drop any existing Summary / Action items sections.
    cleaned = re.sub(
        r"^## Summary\n.*?(?=^## |\Z)", "", md_text, flags=re.M | re.S
    )
    cleaned = re.sub(
        r"^## Action items\n.*?(?=^## |\Z)", "", cleaned, flags=re.M | re.S
    )
    m = re.search(r"^## ", cleaned, flags=re.M)
    if m:
        return (
            cleaned[: m.start()].rstrip()
            + "\n\n" + block + "\n\n"
            + cleaned[m.start():]
        )
    return cleaned.rstrip() + "\n\n" + block + "\n"


def summarize_file(md_path: Path, model: str) -> bool:
    """Read, summarize, and rewrite the transcript in place. False on any
    failure — the transcript is never harmed by a summarizer problem."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"Summarizer could not read {md_path}: {e}")
        return False
    block = generate_summary(text, model)
    if not block:
        return False
    try:
        md_path.write_text(insert_summary(text, block), encoding="utf-8")
        log.info(f"Summary added: {md_path.name}")
        return True
    except Exception as e:
        log.warning(f"Summarizer could not write {md_path}: {e}")
        return False
