"""URL-encoded session state for trivia mini-sessions.

Trivia mini-sessions are stateless on the server: the queue of remaining
cards (`?cards=1,2,3`) and the chain of per-card verdicts (`?done=1r,2w`)
ride along in the URL. This module owns the parse/format/mutate helpers
for those two strings.

Why URL-encoded state instead of server-side session rows: lets the user
refresh, back-button, or tap a notification mid-session without losing
their place. No row to expire, no GC to run. The trade-off is the URL
gets a little longer — for a 3-card session it's e.g.
`?cards=49&done=47r,48w`, which is well within Apple's push-URL budget.
"""

from __future__ import annotations


def parse_card_ids(raw: str | None) -> list[int]:
    """Parse `?cards=1,2,3` into [1, 2, 3]. Empty / None yields []."""
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return out


def parse_done(raw: str | None) -> list[tuple[int, str]]:
    """`done` query param format: `<qid><r|w>,<qid><r|w>,...` e.g.
    `42r,17w,99r`. Carries per-card verdicts forward through the URL
    chain so the end-of-session summary can render the user's run
    without server-side session state. Malformed chunks are dropped
    (defensive against hand-edited URLs)."""
    if not raw:
        return []
    out: list[tuple[int, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if len(chunk) >= 2 and chunk[-1] in ("r", "w") and chunk[:-1].isdigit():
            out.append((int(chunk[:-1]), chunk[-1]))
    return out


def format_done(items: list[tuple[int, str]]) -> str:
    """Inverse of `parse_done`. Used to encode the next-card link's
    `done` param after appending or flipping a verdict."""
    return ",".join(f"{qid}{verdict}" for qid, verdict in items)


def flip_done_verdict(done_items: list[tuple[int, str]], qid: int, correct: bool) -> str:
    """Mutate the carry-forward done chain so the regraded card's
    verdict reflects the new outcome. Called from the session regrade
    flow so the next-card link + summary view see the corrected
    verdict."""
    new_verdict = "r" if correct else "w"
    out = [(q, new_verdict if q == qid else v) for q, v in done_items]
    return format_done(out)
