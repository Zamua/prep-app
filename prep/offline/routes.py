"""HTTP surface for the offline bounded context.

Milestone M1 ships the read-only snapshot; the sync POST arrives in
M2. Both are JSON endpoints authenticated by the standard
current_user dependency (docs/OFFLINE.md section 4). The un-auth-gated
/offline shell itself lives with the PWA routes in prep/web/pwa.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from prep.auth import current_user
from prep.infrastructure.db import now
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
