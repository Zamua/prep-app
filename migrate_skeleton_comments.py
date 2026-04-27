"""One-off migration: strip placeholder/explanatory comments from existing
skeletons. /* ... */, // TODO, // your code here, // fill in, etc. The
prompt explains what to do — the skeleton should carry structure only.

Run from the prep-app dir:
    .venv/bin/python migrate_skeleton_comments.py             # process all
    .venv/bin/python migrate_skeleton_comments.py --dry-run   # preview
    .venv/bin/python migrate_skeleton_comments.py --id 18     # single card
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.sqlite"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))
EMPTY_MCP = '{"mcpServers":{}}'

PROMPT = """You are cleaning a skeleton (starter code) used to prefill the answer textarea of an interview-prep flashcard. The skeleton currently contains placeholder/explanatory comments that add no value because the prompt itself explains what to implement.

Remove these noise comments while preserving structure:
- Slash-star ellipsis comments (/* ... */, /* TODO */, /* fill in */, etc.)
- Single-line placeholder comments (// TODO, // your code here, // fill in, // your fields here)
- Anything whose only purpose is to say "implement this" — the prompt covers that

KEEP:
- Type/struct/class declarations
- Method signatures
- Empty function bodies (curly braces with no content, or open-brace-then-close-brace on separate lines)
- Real comments that explain a non-obvious constraint or invariant (rare)
- Field names that hint at structure ("// your fields here" → DELETE; "// must be sorted" → KEEP)

Current skeleton:
<<<
{skeleton}
>>>

Output a single JSON object (no fences, no prose):
- "changed": true if you removed anything, false if the skeleton was already clean
- "skeleton": the cleaned skeleton (or the original unchanged if changed=false)

Output ONLY the JSON object."""


def clean_via_claude(skeleton: str, timeout: int = 90) -> dict | None:
    proc = subprocess.run(
        [CLAUDE_BIN,
         "--strict-mcp-config", "--mcp-config", EMPTY_MCP,
         "-p", PROMPT.format(skeleton=skeleton)],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        print(f"  ! claude exit {proc.returncode}: {proc.stderr.strip()[:300]}")
        return None
    raw = proc.stdout.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e == -1:
        print(f"  ! no JSON object: {raw[:200]}")
        return None
    try:
        return json.loads(raw[s:e + 1])
    except json.JSONDecodeError as exc:
        print(f"  ! parse error: {exc}: {raw[:200]}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--id", type=int, default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if args.id is not None:
        rows = conn.execute(
            "SELECT id, skeleton FROM questions WHERE id = ? AND skeleton IS NOT NULL AND skeleton != ''",
            (args.id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, skeleton FROM questions "
            " WHERE type='code' AND skeleton IS NOT NULL AND skeleton != ''"
        ).fetchall()

    print(f"Cleaning {len(rows)} skeletons" + (" [dry-run]" if args.dry_run else ""))
    updated = 0
    for r in rows:
        qid = r["id"]
        print(f"\n--- qid={qid} ---")
        result = clean_via_claude(r["skeleton"])
        if not result:
            print("  ! skipped")
            continue
        if not result.get("changed"):
            print("  · already clean")
            continue
        cleaned = result.get("skeleton", "").strip()
        if not cleaned:
            print("  · clean returned empty, skipping")
            continue
        # Show a quick diff sample.
        before_lines = r["skeleton"].splitlines()
        after_lines = cleaned.splitlines()
        print(f"  ✓ {len(before_lines)} → {len(after_lines)} lines")
        # First line that differs:
        for i, (a, b) in enumerate(zip(before_lines, after_lines)):
            if a != b:
                print(f"    line {i + 1} before: {a!r}")
                print(f"    line {i + 1} after:  {b!r}")
                break
        if args.dry_run:
            continue
        with conn:
            conn.execute(
                "UPDATE questions SET skeleton = ? WHERE id = ?", (cleaned, qid),
            )
        updated += 1

    print(f"\nDone. Updated {updated} skeletons.")


if __name__ == "__main__":
    main()
