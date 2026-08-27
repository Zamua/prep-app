"""`GET /metrics` on a running TypeScript node (docs/PHASE-5.md B).

`worker/tests/metrics.test.ts` gates the encoder against
`prometheus_client` byte for byte. This one gates the deployed route:
that the scrape exists, carries the exposition media type, is not
itself counted, and that a request through the real entry worker lands
under the template it matched.

    PARITY_BASE_URL=http://127.0.0.1:8791 \
    PARITY_INTERNAL_TOKEN=parity-internal-token \
    .venv/bin/pytest tests/parity/test_ts_metrics.py -q

Point it at ONE node, not at a fleet ingress. The registry is
module-level, so it belongs to the isolate that answered the scrape:
across a fleet two scrapes reach two isolates and the deltas below are
meaningless. That is a property of the target, not of the test.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from tests.parity.harness.constants import PARITY_USER, PARITY_USER_NAME, internal_token
from tests.parity.harness.server import BASE_URL_ENV

CONTENT_TYPE = "text/plain; version=1.0.0; charset=utf-8"

KEPT = (
    "prep_ai_grade_duration_seconds",
    "prep_instant_generate_duration_seconds",
    "prep_http_request_duration_seconds",
)

# The threadpool gauges and everything `prometheus_client`'s default
# registry shipped for free. A cell has no process to report on, so the
# rewrite drops them; a name reappearing here means one came back by
# accident with the wrong meaning.
GONE = (
    "prep_anyio_threadpool_borrowed",
    "prep_anyio_threadpool_capacity",
    "python_info",
    "python_gc_objects_collected_total",
    "python_gc_objects_uncollectable_total",
    "python_gc_collections_total",
    "process_cpu_seconds_total",
    "process_resident_memory_bytes",
    "process_virtual_memory_bytes",
    "process_start_time_seconds",
    "process_open_fds",
    "process_max_fds",
)


def base_url() -> str:
    url = os.environ.get(BASE_URL_ENV)
    if not url:
        pytest.skip(f"set {BASE_URL_ENV} to a running TypeScript node")
    return url.rstrip("/")


def get(path: str, *, identified: bool = False) -> tuple[int, dict[str, str], str]:
    headers = {"accept": "text/html"}
    if identified:
        headers |= {
            "tailscale-user-login": PARITY_USER,
            "tailscale-user-name": PARITY_USER_NAME,
            "x-internal-token": internal_token(),
        }
    request = urllib.request.Request(f"{base_url()}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            return response.status, _lower(response.headers), body
    except urllib.error.HTTPError as e:
        return e.code, _lower(e.headers), e.read().decode("utf-8")


def _lower(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def scrape() -> tuple[dict[str, str], str]:
    status, headers, body = get("/metrics")
    assert status == 200, body[:400]
    return headers, body


def samples(body: str) -> dict[str, float]:
    """`name{labels} value` lines as a flat map, comments dropped."""
    out: dict[str, float] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        series, _, value = line.rpartition(" ")
        out[series] = float(value)
    return out


def declared(body: str) -> dict[str, str]:
    """`# TYPE <name> <type>` as a map."""
    return {
        parts[2]: parts[3]
        for parts in (line.split() for line in body.splitlines() if line.startswith("# TYPE "))
    }


@pytest.fixture(scope="module")
def body() -> str:
    # Two requests the route table names differently, then a path it does
    # not serve, so the scrape below has all three shapes to report on.
    get("/privacy")
    get("/deck/no-such-deck-here", identified=True)
    get("/not-a-route-at-all")
    return scrape()[1]


def test_the_scrape_is_the_exposition_and_is_not_cached():
    headers, text = scrape()
    assert headers.get("content-type") == CONTENT_TYPE
    assert headers.get("cache-control") == "no-store"
    assert text.startswith("# HELP ")


def test_the_three_kept_families_are_declared_as_histograms(body: str):
    types = declared(body)
    assert [name for name in KEPT if types.get(name) == "histogram"] == list(KEPT)


def test_the_series_a_cell_cannot_produce_are_gone(body: str):
    assert [name for name in GONE if name in body] == []
    # A per-child creation timestamp on a per-isolate counter would read as
    # a reset time that is not one.
    assert "_created" not in body


def test_a_request_lands_under_the_template_it_matched(body: str):
    counts = samples(body)
    entry = 'prep_http_request_duration_seconds_count{method="GET",route="/privacy",status="200"}'
    unmatched = (
        'prep_http_request_duration_seconds_count{method="GET",route="<unmatched>",status="404"}'
    )
    for series in (entry, unmatched):
        assert counts.get(series, 0) >= 1, series
    # A cell route carries its template and not the deck name, whatever the
    # handler answered.
    cell = 'prep_http_request_duration_seconds_count{method="GET",route="/deck/{name}",status='
    assert [k for k in counts if k.startswith(cell)], "no /deck/{name} series"
    assert "no-such-deck-here" not in body


def test_the_scrape_does_not_count_itself():
    before = samples(scrape()[1])
    after = samples(scrape()[1])
    assert 'route="/metrics"' not in "".join(after)
    total = sum(v for k, v in before.items() if k.endswith("}") and "_count{" in k)
    assert sum(v for k, v in after.items() if k.endswith("}") and "_count{" in k) == total
