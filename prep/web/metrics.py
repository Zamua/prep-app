"""Prometheus metrics emission.

Wires four prep-specific signals into a process-local prometheus
registry. Scraped by the obs-stack Prometheus running on the same
docker daemon (see ~/Dropbox/workspace/macmini/observability/).

Why the four below:

- `anyio_threadpool_borrowed` / `anyio_threadpool_capacity`: directly
  answer "are we exhausting the threadpool / leaking threads?" — the
  failure mode that took prod down on 2026-05-07. Sampled lazily on
  every /metrics scrape, so we get a real-time picture without an
  always-on background task.
- `prep_claude_grade_duration_seconds`: histogram of every claude_grade
  call's wall time, tagged with verdict (right/wrong/fallback). Lets
  us see latency tail + correlate with claude-side slowdowns.
- `prep_http_request_duration_seconds`: per-route latency histogram +
  request count. Standard golden-signals view for the FastAPI surface.

The `/metrics` route in prep.app exposes the registry. Prometheus
scrapes it via Tailscale-Serve at /prep-staging/metrics (staging) or
/prep/metrics (prod) — config in observability/prometheus/prometheus.yml.
"""

from __future__ import annotations

import time
from typing import Callable

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Gauge, Histogram, generate_latest

# ---- threadpool -------------------------------------------------------

_THREADPOOL_BORROWED = Gauge(
    "prep_anyio_threadpool_borrowed",
    "Threads currently held by sync route handlers (anyio default limiter). "
    "Approaching capacity = blocking I/O is parking threads faster than they "
    "return; sustained capacity = exhaustion (the 2026-05-07 outage signal).",
)

_THREADPOOL_CAPACITY = Gauge(
    "prep_anyio_threadpool_capacity",
    "Total capacity of the anyio default threadpool. Default 40; constant per "
    "process. Emitted as a gauge for grafana convenience (vs hardcoding the "
    "limit in the dashboard).",
)


def _sample_threadpool() -> None:
    """Snapshot the anyio default threadpool's borrowed/capacity. Called
    on every /metrics scrape so we don't need a polling loop, and so the
    sample is fresh at scrape time rather than up to N seconds stale.

    Has to run inside an asyncio event loop — anyio's `current_default_thread_limiter`
    raises `NoEventLoopError` otherwise. The /metrics route IS async so
    this is fine when called from there.
    """
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        _THREADPOOL_BORROWED.set(limiter.borrowed_tokens)
        _THREADPOOL_CAPACITY.set(limiter.total_tokens)
    except Exception:
        # Don't let an instrumentation failure break /metrics. The
        # remaining metrics still serialize.
        pass


# ---- claude_grade -----------------------------------------------------

_CLAUDE_GRADE_DURATION = Histogram(
    "prep_claude_grade_duration_seconds",
    "Wall-clock duration of one claude_grade call from prompt-build to "
    "response-parsed. Labeled by `verdict` (right/wrong/fallback) so we can "
    "see fallback rate separately from successful grading latency.",
    labelnames=("verdict",),
    # Buckets tuned for the 5-12s normal range with a long tail up to
    # the 12s timeout. Default buckets max out at 10s which clips us.
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 12.0, 15.0, 20.0, 30.0),
)


def observe_claude_grade(*, verdict: str, duration_s: float) -> None:
    """Public hook for prep.trivia.service to report one grading call."""
    _CLAUDE_GRADE_DURATION.labels(verdict=verdict).observe(duration_s)


# ---- HTTP request duration --------------------------------------------

_HTTP_DURATION = Histogram(
    "prep_http_request_duration_seconds",
    "Request handling time per route. Labels are coarse on purpose: "
    "`route` is the FastAPI route template (e.g. /deck/{name}), not the "
    "raw URL — keeps cardinality bounded.",
    labelnames=("method", "route", "status"),
    # Default buckets are fine for the bulk of routes; still extend to
    # 12s to cover the claude-graded outliers.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 7.5, 12.0),
)


async def http_metrics_middleware(request: Request, call_next: Callable):
    """Starlette/FastAPI middleware to record `prep_http_request_duration_seconds`
    per request. Must be installed BEFORE the routers it instruments.
    Skips /metrics itself (don't pollute the histogram with scrape calls)."""
    if request.url.path.endswith("/metrics"):
        return await call_next(request)
    t0 = time.monotonic()
    response: Response | None = None
    try:
        response = await call_next(request)
        return response
    finally:
        elapsed = time.monotonic() - t0
        # `request.scope["route"].path` is the template form (e.g.
        # "/deck/{name}") if a route matched. Fall back to "<unmatched>"
        # so a 404 spam doesn't blow up cardinality on path-templates.
        route = getattr(request.scope.get("route"), "path", None) or "<unmatched>"
        status = str(response.status_code) if response is not None else "500"
        _HTTP_DURATION.labels(
            method=request.method,
            route=route,
            status=status,
        ).observe(elapsed)


# ---- /metrics endpoint ------------------------------------------------


async def metrics_response() -> Response:
    """Body for the GET /metrics route. Samples the threadpool gauges
    just-in-time, then serializes the whole default registry."""
    _sample_threadpool()
    body = generate_latest(REGISTRY)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
