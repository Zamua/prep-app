"""Pin the public surface of prep.temporal_client.

Routes call into temporal_client by name (`temporal_client.foo(...)`).
A previous cleanup pass deleted `describe_workflow` along with some
genuinely-dead helpers, but four polling routes still call it — so
the deletion silently broke /grading and /transform polling on prod
and only surfaced when a user clicked through the flow.

This module greps the codebase for every `temporal_client.<name>`
attribute access and asserts the name resolves on the module. Cheap
and catches the same class of regression — no Temporal server needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import prep.temporal_client as tc

_ROOT = Path(__file__).resolve().parent.parent / "prep"
_PATTERN = re.compile(r"\btemporal_client\.([A-Za-z_][A-Za-z_0-9]*)")


def test_all_referenced_attributes_exist():
    referenced: set[str] = set()
    for path in _ROOT.rglob("*.py"):
        text = path.read_text()
        for match in _PATTERN.finditer(text):
            referenced.add(match.group(1))
    missing = sorted(name for name in referenced if not hasattr(tc, name))
    assert not missing, (
        f"prep/ references temporal_client.{missing} but those names "
        f"are not defined on the module — a callsite will crash at runtime."
    )
