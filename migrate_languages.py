"""One-off backfill: set `language` on existing code questions.

Heuristic-based — looks at the skeleton's first non-blank line, falls back
to the prompt if no skeleton. Default is "go" (most cards are Go right now).

Recognized languages: go, java, python, javascript, typescript, kotlin,
rust, c, cpp.

Run once after the language column is added:
    .venv/bin/python migrate_languages.py
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.sqlite"


def detect_language(skeleton: str | None, prompt: str | None) -> str:
    """Best-effort language detection. Returns a CodeMirror lang id."""
    text = (skeleton or "") + "\n" + (prompt or "")
    text = text.strip()
    if not text:
        return "go"

    # Direct keyword cues — skeletons are short, signals are strong.
    head = "\n".join(line for line in text.splitlines() if line.strip())[:600]

    if re.search(r"\bpackage\s+\w+", head) and ("func " in head or ":= " in head or "chan " in head):
        return "go"
    if re.search(r"\b(public|private|protected)\s+(class|interface)\b", head) or "import java." in head:
        return "java"
    if re.search(r"^def\s+\w+", head, re.MULTILINE) or "self." in head or "import " in head and "from " in head:
        return "python"
    if re.search(r"\bfun\s+\w+", head) or "import kotlin" in head:
        return "kotlin"
    if "fn " in head and ("let " in head or "impl " in head or "->" in head):
        return "rust"
    if re.search(r"\b(function|const|let|var)\s+\w+", head) and ("=>" in head or "{" in head):
        # Check for typescript-specific syntax
        if ": " in head and re.search(r":\s*(string|number|boolean)", head):
            return "typescript"
        return "javascript"
    if "#include" in head:
        if "std::" in head or "namespace" in head:
            return "cpp"
        return "c"

    # Fallback: look for problem framing keywords.
    p = (prompt or "").lower()
    for lang in ("go", "java", "python", "javascript", "typescript", "kotlin", "rust", "c++", "cpp"):
        if f" in {lang}" in p or f" in **{lang}**" in p:
            return "cpp" if lang == "c++" else lang

    return "go"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, prompt, skeleton, language FROM questions "
        " WHERE type='code' AND (language IS NULL OR language = '')"
    ).fetchall()
    print(f"Backfilling language on {len(rows)} code questions")
    for r in rows:
        lang = detect_language(r["skeleton"], r["prompt"])
        with conn:
            conn.execute("UPDATE questions SET language = ? WHERE id = ?", (lang, r["id"]))
        print(f"  qid={r['id']} -> {lang}")


if __name__ == "__main__":
    main()
