"""Tolerant extraction of a q/a JSON array from raw model output.

Pure text-to-data: no I/O, no DB, no framework imports.
"""

from __future__ import annotations

import json
import re


def parse_qa_pairs(stdout: str) -> list[dict]:
    """Tolerant parse: the model sometimes wraps JSON in code fences
    or adds a leading note even when told not to. Strip those, then
    try `json.loads` on the bracket-bounded chunk.
    """
    text = stdout.strip()
    # Strip common code-fence wrappers.
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    # The model occasionally prepends a note despite the JSON-only
    # instruction; take the bracket-bounded chunk.
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end < start:
        raise ValueError("agent output contained no JSON array")
    chunk = text[start : end + 1]
    parsed = json.loads(chunk)
    if not isinstance(parsed, list):
        raise ValueError("agent JSON was not a list")
    return parsed
