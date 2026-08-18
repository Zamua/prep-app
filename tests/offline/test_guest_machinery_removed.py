"""Deletion gate (docs/ANONYMOUS-ACCOUNTS.md sections 8 and 9).

The retired device-local deck flow is gone from the shipped source,
and the `"guest"` meta key survives in exactly one place: the one-time
wipe that removes it. A reintroduced symbol, or a second site naming
the key, fails here.

Scoped to shipped source (static/js, static/css, templates, prep).
Tests are exempt: the wipe's own tests must seed the key to prove the
wipe clears it.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCE_DIRS = ("static/js", "static/css", "templates", "prep")

# Symbols the deleted machinery owned. None may come back anywhere in
# the shipped source.
RETIRED_SYMBOLS = ("guestAdoption", "adoptionApproved", "guestNudge")

# The single site allowed to name the meta keys: the wipe has to say
# what it removes.
WIPE_FILE = Path("static/js/offline/sync.js")
WIPE_FUNCTION = "wipeLegacyGuestData"


def _source_files() -> list[Path]:
    files: list[Path] = []
    for rel in SOURCE_DIRS:
        for path in sorted((REPO_ROOT / rel).rglob("*")):
            if path.is_file() and path.suffix in {".js", ".css", ".html", ".py"}:
                files.append(path)
    assert files, "source tree not found"
    return files


def _wipe_body() -> str:
    """The wipe function's source, from its signature to the closing
    brace in column one."""
    text = (REPO_ROOT / WIPE_FILE).read_text(encoding="utf-8")
    start = text.index(f"export async function {WIPE_FUNCTION}(")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def test_retired_symbols_are_gone_from_the_source():
    offenders = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for symbol in RETIRED_SYMBOLS:
            if symbol in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {symbol}")
    assert offenders == []


def test_the_guest_meta_keys_are_named_only_by_the_wipe():
    """Non-vacuous by construction: the wipe itself is the one file
    the counts below are allowed to be non-zero in, and they are."""
    body = _wipe_body()
    assert body.count('"guest"') == 2  # the gate read, then the delete
    assert body.count('"guest_nudge"') == 1

    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        expected = body if path == REPO_ROOT / WIPE_FILE else ""
        for key in ('"guest"', "'guest'", '"guest_nudge"', "'guest_nudge'"):
            assert text.count(key) == expected.count(key), (
                f"{path.relative_to(REPO_ROOT)} names the {key} meta key"
            )
