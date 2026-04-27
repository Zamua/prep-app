"""Grade a free-text answer against a question's model answer + rubric.

For `mcq` and `multi`, grading is deterministic — no Claude needed.
For `code` and `short`, we shell out to `claude -p` with the rubric.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))


def _grade_freetext(question: dict, user_answer: str, timeout_seconds: int = 90) -> dict:
    rubric = question.get("rubric") or "(no explicit rubric — judge against the model answer)"
    answer_block = user_answer if user_answer else "(blank — user pressed I don't know)"
    prompt = f"""You are grading a flashcard answer for an interview-prep app. Be strict but fair.

**Question type:** {question['type']}
**Prompt:**
{question['prompt']}

**Model answer:**
{question['answer']}

**Rubric (what a correct answer must demonstrate):**
{rubric}

**User's answer:**
{answer_block}

Decide: is the user's answer substantively correct? Partial credit counts as wrong (we'll re-show it soon). For `code` questions, accept any correct approach — don't require the exact syntax of the model answer.

Output a single JSON object (no prose, no fences) with:
- "result": "right" or "wrong"
- "feedback": 1–3 sentences of feedback the user will see. Be concrete: name what they got/missed.
- "model_answer_summary": 1–2 sentence summary of the model answer for the user to compare against.

Output ONLY the JSON object."""

    proc = subprocess.run(
        [CLAUDE_BIN, "-p", prompt],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if proc.returncode != 0:
        return {
            "result": "wrong",
            "feedback": f"(grader error: {proc.stderr.strip()[:300]})",
            "model_answer_summary": question.get("answer", "")[:400],
        }

    raw = proc.stdout.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return {
            "result": "wrong",
            "feedback": f"(grader returned non-JSON: {raw[:300]})",
            "model_answer_summary": question.get("answer", "")[:400],
        }
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        return {
            "result": "wrong",
            "feedback": f"(grader JSON error: {e})",
            "model_answer_summary": question.get("answer", "")[:400],
        }
    if parsed.get("result") not in {"right", "wrong"}:
        parsed["result"] = "wrong"
    return parsed


def grade(question: dict, user_answer: str, idk: bool = False) -> dict:
    if idk:
        return {
            "result": "wrong",
            "feedback": "Marked as 'I don't know' — see again soon.",
            "model_answer_summary": question.get("answer", "")[:400],
        }
    qtype = question["type"]
    if qtype == "mcq":
        correct = (user_answer or "").strip() == (question["answer"] or "").strip()
        return {
            "result": "right" if correct else "wrong",
            "feedback": "Correct." if correct else "Wrong choice.",
            "model_answer_summary": question["answer"],
        }
    if qtype == "multi":
        try:
            picked = set(json.loads(user_answer)) if user_answer else set()
            expected = set(json.loads(question["answer"]))
        except (json.JSONDecodeError, TypeError):
            picked, expected = set(), set()
        correct = picked == expected
        return {
            "result": "right" if correct else "wrong",
            "feedback": "Correct." if correct else f"Expected: {sorted(expected)}; you picked: {sorted(picked)}.",
            "model_answer_summary": str(sorted(expected)),
        }
    return _grade_freetext(question, user_answer)
