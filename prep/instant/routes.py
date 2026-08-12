"""HTTP surface for the instant bounded context.

POST /api/instant/generate - unauthenticated, JSON in/out. The route
is the translation layer: derive the limiter bucket, reserve through
the ledger, call the service, map exceptions to the wire error kinds
and the reservation to its outcome class.

Every response carries a `kind` the client branches on; errors also
carry a human-readable `message`. The endpoint never 500s: unexpected
failures map to `generation_failed` and count as spend.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from functools import partial

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from prep.instant import repo, service
from prep.web.metrics import observe_instant_generate

logger = logging.getLogger(__name__)

router = APIRouter()

_ENV_CLIENT_IP_HEADER = "PREP_CLIENT_IP_HEADER"
_DEFAULT_CLIENT_IP_HEADER = "x-real-ip"
_XFF_LAST_MODE = "x-forwarded-for-last"

# Fail-closed bucket for requests whose client IP cannot be derived;
# shares one set of per-IP windows across every such request.
SENTINEL_BUCKET = "unresolved"

# Body ceiling, far above any legitimate topic payload. Enforced
# before parse: the endpoint is anonymous and the limiter only runs
# after parse, so an unbounded body read is a pre-limiter DoS.
MAX_BODY_BYTES = 16 * 1024

_RATE_LIMITED_COPY = {
    "minute": "One deck a minute. Try again shortly.",
    "day": "You've reached today's limit. Create a free account to keep going.",
}
_ERROR_COPY = {
    "busy": "The free AI is busy right now. Try again in a few minutes.",
    "generation_failed": "That didn't work. Try again.",
    "invalid_topic": "Describe your topic in 1 to 500 characters.",
    "not_configured": "Instant decks aren't available on this deploy.",
}


def client_ip(request: Request) -> str:
    """The limiter bucket for the requesting client.

    Reads only the header named by PREP_CLIENT_IP_HEADER (the
    ingress-set one); `x-forwarded-for-last` selects the LAST
    X-Forwarded-For entry for ingresses that append rather than
    overwrite. A missing or non-IP value fails CLOSED to the shared
    sentinel bucket - never `request.client.host`, which is
    client-forgeable under the deploy's trust-all proxy-header
    config in every deploy shape.

    IPv4 keys on the exact address; IPv6 keys on the /64 prefix (one
    host trivially owns a /64, so exact v6 keys would rotate past the
    per-IP windows for free).
    """
    mode = (os.environ.get(_ENV_CLIENT_IP_HEADER) or _DEFAULT_CLIENT_IP_HEADER).strip().lower()
    if mode == _XFF_LAST_MODE:
        raw = request.headers.get("x-forwarded-for") or ""
        value = raw.rsplit(",", 1)[-1].strip()
    else:
        value = (request.headers.get(mode) or "").strip()
    if not value:
        return SENTINEL_BUCKET
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return SENTINEL_BUCKET
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return str(mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


async def _read_body(request: Request) -> bytes | None:
    """Body bytes, or None when the request must be refused: absent,
    non-integer, or over-cap Content-Length, or a stream that runs
    past the cap (a lying or chunked client)."""
    declared = request.headers.get("content-length")
    try:
        if declared is None or int(declared) > MAX_BODY_BYTES:
            return None
    except ValueError:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _resolve(reservation_id: int, outcome: str, *, cards: int | None = None) -> None:
    """Ledger resolution never changes the response: on failure the
    row stays pending, which already counts as spend (fail closed)."""
    try:
        await anyio.to_thread.run_sync(partial(repo.resolve, reservation_id, outcome, cards=cards))
    except Exception:  # noqa: BLE001 - the visitor keeps their mapped response
        logger.exception("instant generate: resolve failed; row stays pending")


def _error(
    *,
    ip: str,
    started: float,
    status: int,
    kind: str,
    metric: str | None,
    scope: str | None = None,
    retry_after_s: int | None = None,
) -> JSONResponse:
    duration = time.monotonic() - started
    if metric is not None:
        observe_instant_generate(outcome=metric, duration_s=duration)
    payload: dict = {"kind": kind}
    if scope is not None:
        payload["scope"] = scope
        payload["message"] = _RATE_LIMITED_COPY[scope]
    else:
        payload["message"] = _ERROR_COPY[kind]
    headers = None
    if retry_after_s is not None:
        payload["retry_after_s"] = retry_after_s
        headers = {"Retry-After": str(retry_after_s)}
    logger.info(
        "instant generate: ip=%s outcome=%s status=%d duration_ms=%d",
        ip,
        metric or kind,
        status,
        int(duration * 1000),
    )
    return JSONResponse(payload, status_code=status, headers=headers)


@router.post("/api/instant/generate", include_in_schema=False)
async def instant_generate(request: Request) -> JSONResponse:
    """Anonymous free-tier deck generation: one topic in, one deck of
    wire-shaped cards out. The body is parsed manually so every error
    keeps the `kind` shape (FastAPI's validation shape never leaks)."""
    started = time.monotonic()
    ip = client_ip(request)

    try:
        raw = await _read_body(request)
        body = json.loads(raw) if raw is not None else None
    except Exception:  # noqa: BLE001 - malformed, oversized, or aborted bodies refuse alike
        body = None
    topic = service.sanitize_topic(body.get("topic") if isinstance(body, dict) else None)
    if topic is None:
        return _error(ip=ip, started=started, status=422, kind="invalid_topic", metric="invalid")

    agent = service.resolve_agent()
    if agent is None:
        return _error(ip=ip, started=started, status=503, kind="not_configured", metric=None)

    try:
        gate = await anyio.to_thread.run_sync(
            partial(repo.check_and_reserve, ip, topic_chars=len(topic))
        )
    except Exception:  # noqa: BLE001 - a ledger failure refuses; no spend has happened
        logger.exception("instant generate: check_and_reserve failed")
        return _error(ip=ip, started=started, status=429, kind="busy", metric="busy")
    if isinstance(gate, repo.Refusal):
        if gate.kind == "busy":
            return _error(ip=ip, started=started, status=429, kind="busy", metric="busy")
        return _error(
            ip=ip,
            started=started,
            status=429,
            kind="rate_limited",
            metric="rate_limited",
            scope=gate.kind,
            retry_after_s=gate.retry_after_s,
        )

    try:
        deck = await service.generate(agent, topic)
    except service.InstantBusy:
        await _resolve(gate.id, "failed_free")
        return _error(ip=ip, started=started, status=429, kind="busy", metric="failed_free")
    except service.InstantGenerationFailed as e:
        await _resolve(gate.id, "failed_spent")
        logger.info("instant generate failed: ip=%s reason=%s", ip, e)
        return _error(
            ip=ip, started=started, status=502, kind="generation_failed", metric="failed_spent"
        )
    except Exception:
        # Unknown failure after the upstream call may have been issued:
        # count it as spend (fail closed) and keep the wire shape.
        logger.exception("instant generate: unexpected failure")
        await _resolve(gate.id, "failed_spent")
        return _error(
            ip=ip, started=started, status=502, kind="generation_failed", metric="failed_spent"
        )

    await _resolve(gate.id, "ok", cards=len(deck.cards))
    duration = time.monotonic() - started
    observe_instant_generate(outcome="ok", duration_s=duration)
    logger.info(
        "instant generate: ip=%s outcome=ok cards=%d duration_ms=%d",
        ip,
        len(deck.cards),
        int(duration * 1000),
    )
    return JSONResponse({"display_name": deck.display_name, "cards": deck.cards})
