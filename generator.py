"""Generate new questions for a deck via the local agent CLI.

Used as an ad-hoc CLI helper (`.venv/bin/python generator.py <deck> <n>`)
for one-off batches outside the Temporal worker. The web UI's "transform
this deck" flow is the modern path — that runs through the Go worker
with proper retries, idempotency, and observability.

The deck's `context_prompt` (set via the new-deck UI) is what claude
reads. There is no filesystem prep-dir / DECK_CONTEXT fallback anymore;
empty decks are valid (claude returns pure additions).
"""

from __future__ import annotations

import json
import subprocess
import sys

import db
from agent import agent_command


def _build_prompt(deck_name: str, count: int, focus: str, existing_prompts: list[str]) -> str:
    existing_block = "\n".join(f"- {p[:200]}" for p in existing_prompts) or "(none yet)"
    return f"""You are generating flashcard questions for an interview-prep app. The user is preparing for a senior software engineering interview.

**Deck:** {deck_name}

**Focus / context for this deck (provided by the user):**
{focus}

If the description above contains URLs or references recent material, you may use your web-fetch / web-search tools to ground the questions in current information.

**Existing question prompts in this deck — do NOT duplicate or paraphrase any of these:**
{existing_block}

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


def generate(user_id: str, deck_name: str, count: int = 5, timeout_seconds: int = 240) -> list[dict]:
    deck_id = db.get_or_create_deck(user_id, deck_name)
    focus = db.get_deck_context_prompt(user_id, deck_name) or ""
    if not focus.strip():
        raise ValueError(
            f"deck '{deck_name}' has no context_prompt; "
            "set one via the web UI (/decks/new or the deck page) first"
        )
    existing = db.question_prompts_for_deck(user_id, deck_id)
    prompt = _build_prompt(deck_name, count, focus, existing)

    proc = subprocess.run(
        agent_command(prompt),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"agent failed: {proc.stderr.strip()}")

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
        raise ValueError(f"no JSON array in agent output: {raw[:500]}")
    raw = raw[start:end + 1]

    items = json.loads(raw)
    if not isinstance(items, list):
        raise ValueError("expected JSON array")

    inserted: list[dict] = []
    for item in items:
        try:
            qid = db.add_question(
                user_id=user_id,
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
    if len(sys.argv) < 3:
        print("usage: generator.py <user_id> <deck> [count]", file=sys.stderr)
        sys.exit(1)
    uid = sys.argv[1]
    deck = sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    db.init()
    print(f"Generating {n} questions for deck '{deck}' (user={uid})...")
    out = generate(uid, deck, n)
    print(f"Inserted {len(out)} questions:")
    for x in out:
        print(f"  #{x['id']} [{x['type']}] {x['topic']}")
