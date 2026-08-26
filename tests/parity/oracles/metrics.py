"""Metrics oracle: what `prometheus_client` writes for a fixed sequence
of observations against the three families the rewrite keeps.

The corpus is a pair: `sequence.json`, the observations, and
`exposition.txt`, the text `generate_latest` produces from them.
`worker/tests/metrics.test.ts` replays the same sequence through the
TypeScript encoder and compares byte for byte.

Two deliberate narrowings, both because the rewrite has no equivalent
to reproduce:

- The registry holds only the three prep families, in the order
  `prep/web/metrics.py` defines them. The default registry's
  `python_*` and `process_*` collectors, and the two threadpool
  gauges, describe a process that a cell does not have.
- `_created` is off. It is a per-child wall-clock timestamp, so no
  two implementations could agree on it, and on a runtime whose
  counters are per isolate it would read as a reset time that is not
  one.

The histograms are cloned out of the reference module rather than
redeclared, so a bucket boundary or a help string edited there shows
up here as a corpus drift rather than passing unnoticed.
"""

from __future__ import annotations

import prometheus_client
from prometheus_client import CollectorRegistry, Histogram, generate_latest

from tests.parity.oracles import dump_json, write_corpus

NAME = "metrics"

# (family, labels, seconds). Ordering is load-bearing: a child prints in
# the order it was first observed, not sorted.
SEQUENCE: tuple[tuple[str, dict[str, str], float], ...] = (
    # Two observations on one child: a cumulative bucket, a sum that is
    # not a whole number, and a value exactly on a boundary.
    ("http", {"method": "GET", "route": "/", "status": "200"}, 0.012),
    ("http", {"method": "GET", "route": "/", "status": "200"}, 1.0),
    # A second child, first seen after the one that sorts below it.
    ("http", {"method": "POST", "route": "/deck/{name}/split", "status": "303"}, 0.25),
    # Above every finite bound, so only `+Inf` counts it.
    ("http", {"method": "GET", "route": "/deck/{name}", "status": "500"}, 41.5),
    # The label a path no route matched carries, and the smallest bucket.
    ("http", {"method": "GET", "route": "<unmatched>", "status": "404"}, 0.001),
    # Backslash, quote and newline in a label value: not a shape a route
    # produces, and the escaping is a property of the encoder either way.
    ("http", {"method": "GET", "route": 'a\\b"c\nd', "status": "404"}, 0.05),
    ("instant", {"outcome": "ok"}, 30.0),
    ("instant", {"outcome": "rate_limited"}, 0.01),
    # `ai_grade` is deliberately absent: a family with no observation
    # prints its HELP and TYPE and no samples, and the rewrite exposes
    # it in exactly that state.
)


def _clone(source: Histogram, registry: CollectorRegistry) -> Histogram:
    """The same family in a registry of its own. `_upper_bounds` carries
    the implied `+Inf`, which the constructor appends again."""
    return Histogram(
        source._name,
        source._documentation,
        labelnames=source._labelnames,
        buckets=tuple(b for b in source._upper_bounds if b != float("inf")),
        registry=registry,
    )


def exposition(sequence=SEQUENCE) -> str:
    prometheus_client.disable_created_metrics()
    from prep.web import metrics as reference

    registry = CollectorRegistry()
    families = {
        "ai_grade": _clone(reference._AI_GRADE_DURATION, registry),
        "instant": _clone(reference._INSTANT_GENERATE_DURATION, registry),
        "http": _clone(reference._HTTP_DURATION, registry),
    }
    for family, labels, seconds in sequence:
        families[family].labels(**labels).observe(seconds)
    return generate_latest(registry).decode("utf-8")


def extract() -> dict[str, str]:
    return {
        "sequence.json": dump_json(
            [{"family": f, "labels": labels, "seconds": s} for f, labels, s in SEQUENCE]
        ),
        "exposition.txt": exposition(),
    }


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
