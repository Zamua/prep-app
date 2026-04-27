"""Generate new questions for a deck by shelling out to `claude -p`.

Reads:
- the deck's source dir under ~/Dropbox/workspace/interviews/<deck_or_alias>
- shared topic dirs that the deck lists in DECK_CONTEXT
- all existing prompts in the deck (so we can tell the model not to repeat)

Returns a list of dicts ready for db.add_question.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import db

INTERVIEWS_DIR = Path.home() / "Dropbox" / "workspace" / "interviews"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))

# A deck name maps to (its own dir under interviews/, list of shared topic dirs to also read).
# The first dir is treated as the canonical source for the company/role.
DECK_CONTEXT: dict[str, dict] = {
    "cherry": {
        "source": "cherry",
        "topics": ["behavioral"],
        "focus": (
            "Cherry has a multi-round loop: hiring-manager (behavioral, "
            "decision-making, ownership), backend coding in Coderpad, system "
            "design, application/API design, and a final career-narrative round. "
            "Generate a balanced mix biased toward whichever round is closest in "
            "time per the schedule.md if visible. Stack is Kotlin primary, MySQL/"
            "MongoDB/Redis/Elasticsearch, AWS/GCP."
        ),
    },
    "temporal": {
        "source": "temporal-prep",
        "topics": ["concurrency"],
        "focus": (
            "Temporal Senior SWE OSS interview is two back-to-back rounds: "
            "(1) Coding/Concurrency — Go/Java/Python multithreading primitives, "
            "synchronization, deadlock avoidance, channel patterns; "
            "(2) Design/Distributed Systems — durable execution, message queues, "
            "WAL, sharding, consistency, replication. Generate a balanced mix."
        ),
    },
}


def _read_dir_summary(dir_path: Path, max_files: int = 30, max_bytes_per_file: int = 8000) -> str:
    """Concat readable text files under dir_path with size caps."""
    out: list[str] = []
    if not dir_path.exists():
        return ""
    files = sorted(
        [p for p in dir_path.rglob("*") if p.is_file() and p.suffix.lower() in
         {".md", ".txt", ".py", ".go", ".java", ".kt", ".js", ".ts", ".sql", ".yaml", ".yml", ".toml"}]
    )[:max_files]
    for f in files:
        try:
            data = f.read_text(encoding="utf-8", errors="replace")[:max_bytes_per_file]
        except Exception:
            continue
        rel = f.relative_to(dir_path.parent)
        out.append(f"\n--- {rel} ---\n{data}")
    return "".join(out)


def _build_prompt(deck_name: str, count: int, existing_prompts: list[str]) -> str:
    cfg = DECK_CONTEXT.get(deck_name)
    if not cfg:
        raise ValueError(f"Unknown deck '{deck_name}'. Add it to DECK_CONTEXT.")
    source_text = _read_dir_summary(INTERVIEWS_DIR / cfg["source"])
    topic_texts = "\n\n".join(
        f"## Shared topic: {t}\n{_read_dir_summary(INTERVIEWS_DIR / t)}"
        for t in cfg["topics"]
    )
    existing_block = "\n".join(f"- {p[:200]}" for p in existing_prompts) or "(none yet)"

    return f"""You are generating flashcard questions for an interview-prep app. The user is preparing for a senior software engineering interview.

**Deck:** {deck_name}

**Focus / context for this deck:**
{cfg['focus']}

**Existing question prompts in this deck — do NOT duplicate or paraphrase any of these:**
{existing_block}

**Source material (the user's own prep notes for this deck):**
{source_text}

{topic_texts}

---

Generate exactly **{count}** new flashcard questions. Each must be bite-sized but meaningful — answerable in 30 seconds to 5 minutes. Vary the types across the batch.

Output a single JSON array (no prose, no markdown fences). Each element MUST be an object with these fields:

- `type`: one of `"code"`, `"mcq"`, `"multi"`, `"short"`
  - `code`     = write a snippet or pseudocode
  - `mcq`      = single correct answer from a list
  - `multi`    = pick all that apply from a list
  - `short`    = one-or-two-sentence written answer
- `topic`: short string tag (e.g. "concurrency", "behavioral-ownership", "system-design-queue")
- `prompt`: the question text shown to the user (markdown allowed)
- `choices`: array of strings — REQUIRED for `mcq` and `multi`, OMIT for `code` and `short`
- `answer`: the correct answer
  - For `mcq`, the exact correct choice string
  - For `multi`, a JSON-encoded array of the correct choice strings, e.g. `"[\\"A\\", \\"C\\"]"`
  - For `code` and `short`, a model answer (the canonical correct response)
- `rubric`: 2–4 short bullet points describing what a correct answer must demonstrate; used by the grader to evaluate free-text answers

Output ONLY the JSON array, nothing else.
"""


def generate(deck_name: str, count: int = 5, timeout_seconds: int = 240) -> list[dict]:
    deck_id = db.get_or_create_deck(deck_name)
    existing = db.question_prompts_for_deck(deck_id)
    prompt = _build_prompt(deck_name, count, existing)

    proc = subprocess.run(
        [CLAUDE_BIN, "-p", prompt],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed: {proc.stderr.strip()}")

    raw = proc.stdout.strip()
    # Strip optional ```json fences just in case the model adds them.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    # Trim anything before the first '[' or after the last ']'.
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array in claude output: {raw[:500]}")
    raw = raw[start:end + 1]

    items = json.loads(raw)
    if not isinstance(items, list):
        raise ValueError("expected JSON array")

    inserted: list[dict] = []
    for item in items:
        try:
            qid = db.add_question(
                deck_id=deck_id,
                qtype=item["type"],
                prompt=item["prompt"],
                answer=item["answer"],
                topic=item.get("topic"),
                choices=item.get("choices"),
                rubric=item.get("rubric"),
            )
            inserted.append({"id": qid, "type": item["type"], "topic": item.get("topic")})
        except Exception as e:
            print(f"skipping malformed item: {e}: {item!r}")

    return inserted


if __name__ == "__main__":
    import sys
    deck = sys.argv[1] if len(sys.argv) > 1 else "cherry"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    db.init()
    print(f"Generating {n} questions for deck '{deck}'...")
    out = generate(deck, n)
    print(f"Inserted {len(out)} questions:")
    for x in out:
        print(f"  #{x['id']} [{x['type']}] {x['topic']}")
