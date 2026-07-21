"""HTTP surface for the offline bounded context.

Two JSON endpoints, both authenticated by the standard current_user
dependency (docs/OFFLINE.md section 4): the read-only snapshot (M1)
and the sync POST that replays queued offline work through the real
scheduler (M2). The un-auth-gated /offline shell itself lives with
the PWA routes in prep/web/pwa.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from prep.auth import current_user
from prep.infrastructure.db import now
from prep.offline import service
from prep.offline.entities import SyncRequest
from prep.offline.repo import SnapshotRepo

router = APIRouter()


@router.get("/api/offline/snapshot", include_in_schema=False)
def offline_snapshot(user: dict = Depends(current_user)) -> JSONResponse:
    """The full client-side study snapshot: the server-resolved
    identity, the user's SRS decks, and every non-suspended card with
    its SRS view. sync.js writes this into IndexedDB on authenticated
    page loads so the offline shell has data to render.

    The identity in the payload is display-only on the client (the
    "Studying as ..." line and ownership stamping); the sync endpoint
    never trusts a client-side identity claim.
    """
    uid = user["tailscale_login"]
    repo = SnapshotRepo()
    return JSONResponse(
        {
            "user": {"id": uid, "display_name": user.get("display_name") or uid},
            "generated_at": now(),
            "decks": [d.model_dump() for d in repo.decks(uid)],
            "cards": [c.model_dump() for c in repo.cards(uid)],
        }
    )


@router.post("/api/offline/sync", include_in_schema=False)
def offline_sync(batch: SyncRequest, user: dict = Depends(current_user)) -> JSONResponse:
    """Replay a batch of queued offline work (new cards, then
    reviews) under the authenticated user. The session on THIS
    request is the identity; any client-side ownership claim is
    ignored.

    Per-item semantics live in the service: idempotency by
    (user, client_id), cards-before-reviews, timestamp-ordered FSRS
    replay with last-writer-wins, clock clamping, and per-item
    rejection (a bad item never 4xxs the batch). Batch caps are the
    one parse-level rule: an over-cap request 422s, since the client
    always chunks under them."""
    uid = user["tailscale_login"]
    result = service.sync_batch(uid, batch)
    # exclude_none keeps the wire shape minimal: an applied review is
    # {client_id, status}; only rejects carry error, only created
    # cards carry question_id.
    return JSONResponse(result.model_dump(exclude_none=True))
