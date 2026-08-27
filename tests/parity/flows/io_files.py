"""The upload bodies the three importer flows post.

Built here rather than committed, except the `.apkg`, which is one of the
generated corpus files under `tests/parity/goldens/deckio/`: no third-party
deck enters the tree, and every byte a flow uploads is deterministic, so the
two targets import the same rows.
"""

from __future__ import annotations

import io
import json
import zipfile

from tests.parity.harness.constants import GOLDENS_ROOT

# The `.apkg` the deckio corpus generates. Reading it here keeps one source
# for the bytes both gates use.
APKG_PATH = GOLDENS_ROOT / "deckio" / "anki-legacy.apkg"

CSV_HEADER = (
    "type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation\r\n"
)

CARDS_HEADER = (
    CSV_HEADER.rstrip("\r\n") + ",step,next_due,last_review,stability,difficulty,fsrs_state\r\n"
)

REVIEWS_HEADER = "prompt,ts,result,user_answer,grader_notes\r\n"


def csv_body() -> bytes:
    """Three rows that import and one the reader names, so the outcome
    shot carries both counters and the error list."""
    rows = [
        "short,graphs,What does a topological sort require of its input?,A directed acyclic graph,,,,,,\r\n",
        'mcq,graphs,Which traversal visits every neighbour before descending?,Breadth-first,"Depth-first\nBreadth-first",,,,,\r\n',
        "short,graphs,What does a union-find structure answer?,Whether two elements share a set,,,,,,\r\n",
        "essay,graphs,Explain Dijkstra in prose.,It relaxes edges by distance,,,,,,\r\n",
    ]
    return (CSV_HEADER + "".join(rows)).encode("utf-8")


def prepdeck_body() -> bytes:
    """A minimal archive: two cards, one with FSRS state and a review."""
    meta = {
        "format_version": 1,
        "exported_at": "2026-03-14T15:00:00Z",
        "deck": {
            "name": "graph-theory",
            "deck_type": "srs",
            "context_prompt": "Graphs, traversal and shortest paths.",
            "notification_interval_minutes": None,
            "trivia_session_size": 3,
            "desired_retention": None,
        },
    }
    cards = CARDS_HEADER + (
        "short,graphs,How many edges does a tree on n nodes have?,n - 1,,,,,,,"
        "2,2026-03-20T00:00:00+00:00,2026-03-12T00:00:00+00:00,7.5,4.25,2\r\n"
        "short,graphs,What does Dijkstra assume about edge weights?,They are non-negative,,,,,,,"
        ",,,,,\r\n"
    )
    reviews = REVIEWS_HEADER + (
        "How many edges does a tree on n nodes have?,2026-03-12T00:00:00+00:00,right,n - 1,\r\n"
    )
    buf = io.BytesIO()
    # Stored and stamped the way prep's own writer stamps, so the bytes are
    # the same on every machine.
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, body in (
            ("meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n"),
            ("cards.csv", cards),
            ("reviews.csv", reviews),
        ):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zf.writestr(info, body)
    return buf.getvalue()


def apkg_body() -> bytes:
    return APKG_PATH.read_bytes()
