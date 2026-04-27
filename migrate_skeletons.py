"""One-off migration: extract baked-in skeletons from existing code questions.

Walks every `code` question with NULL/empty `skeleton` column, asks Claude
to detect whether the prompt has starter-code scaffolding embedded, and if
so extracts it into the new `skeleton` field while rewriting the prompt
without it.

Run from the prep-app dir:
    .venv/bin/python migrate_skeletons.py            # process all
    .venv/bin/python migrate_skeletons.py --dry-run  # see what would change
    .venv/bin/python migrate_skeletons.py --id 21    # single card

Uses --strict-mcp-config when shelling out so it doesn't compete with the
channel-mode Claude's Telegram MCP (same lesson as the worker activities).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.sqlite"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))
EMPTY_MCP = '{"mcpServers":{}}'


PROMPT_TEMPLATE = """You are reviewing a flashcard question for an interview-prep app to decide whether it has starter code (a "skeleton") baked into the prompt that should be extracted into a separate field.

Current prompt:
<<<
{prompt}
>>>

A skeleton is starter code — type definitions plus method stubs with TODO/blank comments — that the user is meant to fill in. It's characteristic of LeetCode's concurrency series (1114 Print in Order, 1115 Print FooBar Alternately, 1116 Print Zero Even Odd, 1117 Building H2O, 1195 Fizz Buzz Multithreaded, 1226 Dining Philosophers) and similar threading exercises where the canonical version of the problem provides scaffolding.

Output a single JSON object (no markdown fences, no prose):
- "has_skeleton": true if the prompt contains a baked-in skeleton, false otherwise
- "skeleton": the extracted skeleton code as a string (just the code, no fences). Empty string if has_skeleton is false. Keep formatting/indentation. Do NOT include the surrounding "Fill in:" preamble or markdown fences.
- "cleaned_prompt": the prompt rewritten without the skeleton block and without any "Fill in:" preamble. Should describe the problem and end with something like "Implement the methods." or "Fill in the methods." instead of dumping code. If has_skeleton is false, return cleaned_prompt unchanged from the original.

Output ONLY the JSON object."""


def claude_extract(prompt_text: str, timeout: int = 120) -> dict | None:
    """Invoke claude -p; return parsed JSON or None on failure."""
    body = PROMPT_TEMPLATE.format(prompt=prompt_text)
    try:
        proc = subprocess.run(
            [CLAUDE_BIN,
             "--strict-mcp-config", "--mcp-config", EMPTY_MCP,
             "-p", body],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        print(f"  ! claude exit {proc.returncode}: {proc.stderr.strip()[:300]}")
        return None
    raw = proc.stdout.strip()
    # Strip optional fences and trim to {...}.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    s = raw.find("{")
    e = raw.rfind("}")
    if s == -1 or e == -1:
        print(f"  ! no JSON object in response: {raw[:200]}")
        return None
    try:
        return json.loads(raw[s:e + 1])
    except json.JSONDecodeError as exc:
        print(f"  ! JSON parse error: {exc}: {raw[:200]}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would change without writing.")
    ap.add_argument("--id", type=int, default=None,
                    help="Process a single question id (skip the where-clause).")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.id is not None:
        rows = conn.execute(
            "SELECT id, prompt, skeleton FROM questions WHERE id = ?", (args.id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, prompt, skeleton FROM questions "
            " WHERE type='code' AND (skeleton IS NULL OR skeleton = '')"
        ).fetchall()

    print(f"Processing {len(rows)} code questions"
          + (f" (id={args.id})" if args.id else "")
          + (" [dry-run]" if args.dry_run else ""))

    changed = 0
    for r in rows:
        qid = r["id"]
        print(f"\n--- qid={qid} ---")
        result = claude_extract(r["prompt"])
        if not result:
            print("  ! skipped (no result)")
            continue
        if not result.get("has_skeleton"):
            print("  · no skeleton detected, leaving alone")
            continue
        skel = result.get("skeleton", "").strip()
        cleaned = result.get("cleaned_prompt", "").strip()
        if not skel:
            print("  · has_skeleton=true but skeleton empty, skipping")
            continue
        print(f"  ✓ skeleton extracted ({len(skel)} chars)")
        print(f"    skeleton head: {skel.splitlines()[0] if skel else ''!r}")
        print(f"    cleaned prompt head: {(cleaned or r['prompt'])[:80]!r}")
        if args.dry_run:
            continue
        # Update.
        with conn:
            conn.execute(
                "UPDATE questions SET skeleton = ?, prompt = ? WHERE id = ?",
                (skel, cleaned or r["prompt"], qid),
            )
        changed += 1

    print(f"\nDone. Updated {changed} questions.")


if __name__ == "__main__":
    main()
