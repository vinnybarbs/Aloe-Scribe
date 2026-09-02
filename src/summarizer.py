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

_PROMPT = """You are writing the executive summary block for a meeting transcript. Rules: plain business English. Never use em dashes, semicolons, arrows, or the words leverage, utilize, robust, seamless, streamline, holistic, foster, delve. Lead with the point. Use the speaker names that appear in the transcript. Never reproduce raw spoken phrasing as an item, describe the step in plain terms. The Notes section, when present, is the user's own notes and outranks the dialogue for what mattered.

You will be given an extracted list of commitments and threads, then the full document. The extracted list is your source of truth for the Decisions, Action items, and Open questions sections.

Produce exactly four sections and nothing else:
## Summary
4 to 7 short bullets, every bullet starting with "- ". Never paragraph form. Cover what the meeting was about, where each major thread stands, and what was learned.
## Decisions
A decision is a choice the participants made in THIS meeting about what they will do. Background facts, org news, and things learned are not decisions and belong in the Summary. When something was considered and REJECTED, state the rejection: "Not to open the folder to all staff." Recording a rejected option as if it was adopted is the worst possible error. State each decision directly, never beginning with "It was decided that", "It is settled that", or "The team". If nothing was decided, write exactly: None captured.
## Action items
Only lines tagged [commitment] in the extracted list qualify, never [topic] lines: topics discussed and ideas explored are not action items. At most 8 items, the most consequential. Prefer items where someone agreed to set up, share, schedule, or reach out. Each item must be SPECIFIC: name who a call or introduction is with and what it is about, even when the responsible owner is unclear. Never merge separate workstreams into one item, never write "the speaker", describe the step itself. Include a deadline only when one was stated in the meeting. {owner_rule} If there are no commitments, write exactly: None captured.
## Open questions
Unresolved questions the meeting raised or left open, one line each. If none, write exactly: None captured.

Document:
"""

_OWNER_RULE_LABELED = (
    "The owner of an action item is the SPEAKER LABEL on the transcript line "
    "where the commitment is stated, copied exactly. Anonymous labels like R2 "
    "or M1 are valid owners: write \"R2 to send the paperwork\". Never move a "
    "commitment to a different person: when R2 says \"we will send it over\", "
    "the owner is R2, not the named person they are talking to. A speaker "
    "offering or requesting something owns that offer or request. The meeting "
    "title and attendee list are never evidence of ownership. When the owner "
    "is not certain, start the line with \"Owner unclear:\" instead."
)
_OWNER_RULE_UNLABELED = (
    "This transcript does not identify who is speaking, so ownership cannot "
    "be asserted. Start EVERY action item with \"Owner unclear:\" — but DO "
    "name the specific people involved in the step itself (who a call or "
    "introduction is with, who was said to take it on), phrased as "
    "\"Name to do X\" rather than as a claim of ownership."
)

_WORKER = r"""
import json
import sys
model_path = sys.argv[1]
job = json.loads(sys.stdin.read())
from mlx_lm import load, generate
model, tokenizer = load(model_path)

def run(prompt, max_tokens):
    messages = [{"role": "user", "content": prompt}]
    text = tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, enable_thinking=False
    )
    return generate(model, tokenizer, prompt=text, max_tokens=max_tokens, verbose=False)

# Map: extract facts from SHORT transcript windows — a 4B is reliable over
# ten minutes of dialogue and loses details across forty-five.
extractions = []
for chunk in job["chunks"]:
    extractions.append(run(job["extract"] + chunk, 350))
evidence = "\n".join(e.strip() for e in extractions if e.strip())
# Reduce: write the block from the combined evidence plus the document head
# (title, metadata, the user's notes) — a small, dense final context.
final = run(
    job["final"]
    + "\n\nExtracted commitments and threads (your source of truth):\n"
    + evidence
    + "\n\nMeeting metadata and the user's own notes:\n"
    + job["head"],
    1400,
)

# The user's typed notes are ground truth for the judges — but only the
# note text itself: attaching the whole metadata head biased every verdict
# to "contradicted" in testing.
import re as _re
_notes_m = _re.search(r"## Notes\n(.*?)(?=^## |\Z)", job["head"], _re.M | _re.S)
user_notes = (_notes_m.group(1).strip() if _notes_m else "").strip() or "(none)"

# Verify pass: small models keep promoting background facts to "decisions".
# Ask about each candidate line and drop the ones that fail.
m = _re.search(r"^## Decisions\n(.*?)(?=^## |\Z)", final, flags=_re.M | _re.S)
if m:
    kept = []
    for line in m.group(1).splitlines():
        s = line.strip().lstrip("-*• ").strip()
        if not s:
            continue
        if s == "None captured.":
            kept = []
            break
        # Classification framing beats yes/no here: small models have a
        # strong yes-bias when asked "did they agree?", but sort fact from
        # decision reliably when shown examples of each.
        verdict = run(
            "Classify the statement as fact or decision.\n"
            "A decision is a course of action the meeting participants "
            "chose during the meeting. A fact is background information, "
            "org news, an opinion, or something merely learned.\n\n"
            "Examples of fact: 'The company has its own CEO, named Sam.' "
            "'They use Grafana, Splunk, and DataDog.' 'The budget was cut "
            "last quarter.'\n"
            "Examples of decision: 'Focus the proposal on discovery work "
            "first.' 'Alex will handle the technical side while Kim owns "
            "the business case.'\n\n"
            "Statement: " + s
            + "\n\nMeeting evidence:\n" + evidence
            + "\n\nAnswer with exactly one word, fact or decision.",
            10,
        )
        if not verdict.strip().lower().startswith("decision"):
            continue
        # Polarity check: a REJECTED option is still "a decision", so the
        # gate above passes it — but stating it as adopted inverts what the
        # meeting chose, the worst error a summary can make. Ask directly.
        polarity = run(
            "According to the meeting evidence, did the participants ADOPT "
            "this course of action, REJECT it (they discussed it and decided "
            "not to do it), or leave it OPEN with no decision?\n\n"
            "Examples:\n"
            "'Open the folder to all staff' where the evidence shows they "
            "said let's not open it: rejected.\n"
            "'Keep access restricted to the current team' where the evidence "
            "shows they agreed to keep it restricted: adopted (choosing to "
            "keep or limit something IS adopting that choice).\n"
            "'Not to migrate this quarter' where the evidence shows they "
            "decided against migrating: adopted (the statement already says "
            "what they chose).\n"
            "'Switch to the new tool' where nobody concluded anything: open."
            "\n\nStatement: " + s
            + "\n\nEvidence:\n" + evidence
            + "\n\nAnswer with exactly one word: adopted, rejected, or open.",
            10,
        ).strip().lower()
        if polarity.startswith("rejected"):
            fixed = run(
                "The meeting considered this and decided AGAINST it: " + s
                + "\n\nRewrite as one plain line stating what was decided "
                "against, beginning with \"Not to\". Output only the line.",
                60,
            ).strip().splitlines()[0].strip().lstrip("-*• ").strip()
            if fixed:
                kept.append("- " + fixed)
            continue
        if polarity.startswith("open"):
            continue
        kept.append("- " + s)
    body = "\n".join(kept) if kept else "None captured."
    final = final[: m.start(1)] + body + "\n\n" + final[m.end(1):]

# Faithfulness pass over Summary and Action items: the polarity guard on
# Decisions is not enough — a reversed plan can leak back in as a summary
# bullet or an action item. Drop lines the evidence contradicts; keep
# accurate and merely-unsure lines so paraphrase is not punished.
for section in ("Summary", "Action items"):
    m = _re.search(
        r"^## " + section + r"\n(.*?)(?=^## |\Z)", final, flags=_re.M | _re.S
    )
    if not m:
        continue
    kept = []
    for line in m.group(1).splitlines():
        s = line.strip().lstrip("-*• ").strip()
        if not s or s == "None captured." or s.lower().startswith("attendees:"):
            kept.append(line)
            continue
        verdict = run(
            "Meeting evidence:\n" + evidence
            + "\n\nThe user's own typed notes. These are ground truth and "
            "OVERRIDE the dialogue when they conflict:\n" + user_notes
            + "\n\nNow check ONE statement from a draft summary against "
            "the evidence and notes above.\n"
            "Answer 'contradicted' ONLY if the notes or evidence show the "
            "participants rejected, reversed, or walked back what this "
            "specific statement asserts. Answer 'accurate' if it is "
            "supported. Answer 'unsure' if it is neither supported nor "
            "reversed.\n\nStatement: " + s
            + "\n\nAnswer with exactly one word: accurate, contradicted, "
            "or unsure.",
            10,
        ).strip().lower()
        if verdict.startswith("contradicted"):
            continue
        kept.append(line)
    final = final[: m.start(1)] + "\n".join(kept).strip() + "\n\n" + final[m.end(1):]
sys.stdout.write(final)
"""

_EXTRACT_PROMPT = """From the meeting document below, extract facts one line each, naming the SPECIFIC people involved and the specific subject, quoting or closely paraphrasing the transcript. When transcript lines carry speaker labels (names or codes like R2, M1), attribute each fact to the label on the line that states it, copied exactly: "R6 offers to send Carl the mandate document". Never reattribute a statement to a different speaker. Prefix every line with exactly one tag:
[commitment] an agreed next step, planned call, or introduction someone will actually do
[decision] something now settled, INCLUDING options considered and rejected — a rejection must be extracted as "decided NOT to ..." and never as if the option was adopted
[deadline] a stated date or timeframe
[question] an unresolved question
[topic] a subject discussed or an idea explored with no agreement to act
Extraction only, no interpretation, no filler.

Document:
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


def _split_for_extraction(doc: str, chunk_chars: int = 12_000) -> tuple:
    """(head, transcript_chunks): head is everything before the dialogue
    (frontmatter, title, metadata, notes); the dialogue splits into
    ~10-minute windows on line boundaries. Short windows are where a 4B
    extracts reliably — across a whole hour it loses the middle."""
    import re as _re

    m = _re.search(r"^## Transcript$", doc, flags=_re.M)
    if m:
        head, transcript = doc[: m.start()], doc[m.end():]
    else:
        d = _re.search(r"^\[\d+:\d\d\] ", doc, flags=_re.M)
        if d:
            head, transcript = doc[: d.start()], doc[d.start():]
        else:
            return doc, [doc]
    lines = transcript.splitlines(keepends=True)
    overlap_chars = 800
    chunks, cur, size = [], [], 0
    for line in lines:
        cur.append(line)
        size += len(line)
        if size >= chunk_chars:
            chunks.append("".join(cur))
            # Overlap: carry the tail lines into the next window so a
            # thought spanning the boundary appears whole in at least one.
            tail, tail_size = [], 0
            for prev in reversed(cur):
                tail.insert(0, prev)
                tail_size += len(prev)
                if tail_size >= overlap_chars:
                    break
            cur, size = tail, tail_size
    if cur and (not chunks or "".join(cur) != chunks[-1][-len("".join(cur)):]):
        chunks.append("".join(cur))
    return head, chunks or [transcript]


def _summary_is_bulleted(text: str) -> bool:
    """Structural lint: the Summary section must be bullets, not prose."""
    m = re.search(r"^## Summary\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    if not m:
        return False
    lines = [
        l for l in m.group(1).splitlines()
        if l.strip() and not l.startswith("Attendees:")
    ]
    bullets = [l for l in lines if l.lstrip().startswith(("-", "*", "•"))]
    return len(bullets) >= 3 and len(bullets) >= len(lines) - 1


def generate_summary(md_text: str, model: str) -> Optional[str]:
    """Run the model over the document; returns the '## Summary ...' block or
    None on any failure. Subprocess-isolated, bounded at five minutes."""
    import sys as _sys

    if _sys.platform == "win32":
        # The summarizer rides mlx-lm, which is Apple Silicon only. Windows
        # transcripts ship without the summary block until a llama.cpp or
        # ONNX worker lands.
        log.info("Summarizer unavailable on Windows (mlx is Apple-only).")
        return None
    exe = _worker_python()
    if exe is None:
        log.info("No runtime venv found. Summarizer disabled.")
        return None
    # Re-summarizing an already-summarized file must not feed the model its
    # own previous block — it parrots it back verbatim instead of rereading
    # the transcript. Generate from the document minus generated sections.
    doc = md_text
    for section in ("Summary", "Decisions", "Action items", "Open questions"):
        doc = re.sub(
            rf"^## {section}\n.*?(?=^## |\Z)", "", doc, flags=re.M | re.S
        )
    if len(doc) > MAX_INPUT_CHARS:
        half = MAX_INPUT_CHARS // 2
        doc = (
            doc[:half]
            + "\n\n[transcript truncated for length]\n\n"
            + doc[-half:]
        )
    # A transcript without speaker-labeled lines can never yield named
    # owners — small models otherwise invent them from the title or roster.
    has_speakers = bool(
        re.search(r"^\[\d+:\d\d\] [A-Za-z][\w .'-]*?: ", md_text, flags=re.M)
    )
    prompt = _PROMPT.format(
        owner_rule=_OWNER_RULE_LABELED if has_speakers else _OWNER_RULE_UNLABELED
    )
    head, chunks = _split_for_extraction(doc)

    def _attempt(final_prompt: str) -> Optional[str]:
        try:
            import json as _json

            from speakers import worker_env

            proc = subprocess.run(
                [exe, "-c", _WORKER, model],
                input=_json.dumps(
                    {
                        "extract": _EXTRACT_PROMPT,
                        "final": final_prompt,
                        "head": head,
                        "chunks": chunks,
                    }
                ),
                capture_output=True,
                text=True,
                timeout=420,
                env=worker_env(),
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
        return proc.stdout

    out = _attempt(prompt)
    if out is not None and not _summary_is_bulleted(out):
        log.info("Summary came back as prose. Retrying with format reminder.")
        retry = _attempt(
            prompt
            + "\nREMINDER: the Summary section MUST be 4 to 7 bullets, each "
            "line starting with \"- \". Paragraph form is a failure.\n"
        )
        if retry is not None and _summary_is_bulleted(retry):
            out = retry
    if out is None:
        return None
    # Strip any preamble the model produced before the block.
    idx = out.find("## Summary")
    if idx < 0:
        log.warning("Summarizer output had no Summary section. Discarded.")
        return None
    block = out[idx:].strip()
    # Cut anything trailing after the expected sections.
    m = re.search(
        r"^## (?!Summary|Decisions|Action items|Open questions)",
        block,
        flags=re.M,
    )
    if m:
        block = block[: m.start()].strip()
    # The model sometimes loops, emitting the same items several times
    # (repetitive transcripts trigger it). Keep the first copy of each line.
    seen_lines: set = set()
    kept_lines = []
    for line in block.splitlines():
        key = line.strip().lower().rstrip(".")
        if (
            key
            and not line.lstrip().startswith("##")
            and key != "none captured"
        ):
            if key in seen_lines:
                continue
            seen_lines.add(key)
        kept_lines.append(line)
    block = "\n".join(kept_lines)
    # If generation hit the token cap the block ends mid-sentence. Drop a
    # trailing line with no terminal punctuation rather than shipping it.
    tail = block.rstrip().rsplit("\n", 1)
    if len(tail) == 2 and tail[1].strip() and not tail[1].rstrip().endswith(
        (".", "!", "?", ":", '"', "'", ")")
    ) and not tail[1].lstrip().startswith("##"):
        block = tail[0].rstrip()
    # Strip narration prefixes the model keeps sneaking in ("It is settled
    # that X" reads as machine hedging; "X" is the statement).
    block = re.sub(
        r"(?m)^(\s*(?:[-*•]\s*)?)(?:It (?:is|was) (?:settled|decided|agreed)"
        r"(?: that| to)?[,:]?\s+|The (?:team|group) (?:decided|agreed) to\s+)(\w)",
        lambda mm: mm.group(1) + mm.group(2).upper(),
        block,
    )
    # "It is unresolved whether X" is a question wearing a trench coat:
    # "Whether X" matches the clean bullets around it.
    block = re.sub(
        r"(?m)^(\s*(?:[-*•]\s*)?)It (?:is|was|remains) unresolved,?\s+(\w)",
        lambda mm: mm.group(1) + mm.group(2).upper(),
        block,
    )
    if not has_speakers:
        # Small models keep attaching names anyway (usually lifted from the
        # meeting title). Enforce ownerlessness mechanically — scoped to the
        # Action items section so Decisions bullets are untouched.
        m = re.search(r"^## Action items\n(.*?)(?=^## |\Z)", block, flags=re.M | re.S)
        if m:
            name = r"[A-Z][\w.'-]*(?: [A-Z][\w.'-]*){0,3}"
            section = m.group(1)
            # Reframe "Name will X" as "Name to X" under the prefix —
            # the name stays as PARTICIPANT info, just not as an owner claim.
            section = re.sub(
                rf"(?m)^(\s*[*-]?\s*)Owner unclear:\s*({name}) will\s+",
                r"\1Owner unclear: \2 to ",
                section,
            )
            section = re.sub(
                rf"(?m)^(\s*[*-]?\s*)({name}) will\s+",
                r"\1Owner unclear: \2 to ",
                section,
            )
            # "The speaker" is banned but persistent — collapse it into the
            # ownerless prefix: "Owner unclear: The speaker agrees to share X"
            # becomes "Owner unclear: share X".
            section = re.sub(
                r"(?mi)^(\s*[*-]?\s*Owner unclear:\s*)[Tt]he speaker\s+"
                r"(?:proposes|commits? to|agrees? to|is (?:to|assigned to)|will|offers? to)\s*",
                r"\1",
                section,
            )
            block = block[: m.start(1)] + section + block[m.end(1):]
    return block


def _attendees_line(md_text: str) -> Optional[str]:
    # Prefer the body metadata line: it carries Obsidian wikilinks when the
    # vault mode is on. Frontmatter (always plain names) is the fallback.
    m = re.search(r"^Attendees: (.+)$", md_text, flags=re.M)
    if m and m.group(1).strip():
        return f"Attendees: {m.group(1).strip()}"
    m = re.search(r"^attendees: \[([^\]]*)\]$", md_text, flags=re.M)
    if not m or not m.group(1).strip():
        return None
    return f"Attendees: {m.group(1).strip()}"


def insert_summary(md_text: str, block: str) -> str:
    """Place the Summary/Action items block after the document's metadata,
    before the first existing section. Idempotent: an earlier summary block
    is replaced, so re-summarizing never stacks duplicates.

    The attendee roster is repeated as the first line INSIDE the Summary
    section — people copy-paste just the summary, and it should carry who
    was in the meeting."""
    attendees = _attendees_line(md_text)
    if attendees and block.startswith("## Summary"):
        head, _, rest = block.partition("\n")
        block = f"{head}\n{attendees}\n{rest.lstrip()}"
    # Drop any existing generated sections so re-summarizing replaces them.
    cleaned = md_text
    for section in ("Summary", "Decisions", "Action items", "Open questions"):
        cleaned = re.sub(
            rf"^## {section}\n.*?(?=^## |\Z)", "", cleaned, flags=re.M | re.S
        )
    m = re.search(r"^## ", cleaned, flags=re.M)
    if m:
        return (
            cleaned[: m.start()].rstrip()
            + "\n\n" + block + "\n\n"
            + cleaned[m.start():]
        )
    # Legacy layout with no section headers (old fallback files): the block
    # goes ABOVE the dialogue, never appended below it.
    m = re.search(r"^\[\d+:\d\d\] ", cleaned, flags=re.M)
    if m:
        return (
            cleaned[: m.start()].rstrip()
            + "\n\n" + block + "\n\n## Transcript\n\n"
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
