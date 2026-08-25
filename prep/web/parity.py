"""`PREP_PARITY_MODE=1`: the pins that let two servers render the
same bytes for the parity gate. Never set in a deploy file.

Under the flag base.html omits ClerkJS, the vendor doc shells lose
their cross-origin script and stylesheet tags, and the `/_parity/*`
routes exist: the seed, a deliberate error, and the two shells only
a Clerk session state can otherwise reach.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from prep.web.templates import templates

ENV_PARITY_MODE = "PREP_PARITY_MODE"


def parity_mode() -> bool:
    return os.environ.get(ENV_PARITY_MODE, "").strip() == "1"


# A whole external script element, or a link element; the attribute
# that names the resource is captured for the host check.
_SCRIPT_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*([\"'])(?P<url>[^\"']*)\1[^>]*>\s*</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_LINK_RE = re.compile(
    r"<link\b[^>]*\bhref\s*=\s*([\"'])(?P<url>[^\"']*)\1[^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)


def _is_cross_origin(url: str, host: str) -> bool:
    netloc = urlsplit(url.strip()).netloc
    return bool(netloc) and netloc.lower() != host.lower()


def strip_cross_origin_tags(html: str, host: str) -> str:
    """Drop every `<script src>` and `<link href>` whose host is not
    `host`. Relative and same-host URLs stay; inline scripts stay."""

    def _drop(match: re.Match) -> str:
        return "" if _is_cross_origin(match.group("url"), host) else match.group(0)

    html = _SCRIPT_RE.sub(_drop, html)
    return _LINK_RE.sub(_drop, html)


router = APIRouter()


@router.get("/_parity/raise", include_in_schema=False)
def parity_raise(status: int = 500) -> None:
    """A deliberate error page: 500 by default, 429 on request."""
    if status == 429:
        raise HTTPException(429, "parity: deliberate throttle")
    raise RuntimeError("parity: deliberate server error")


@router.get("/_parity/reauth", response_class=HTMLResponse, include_in_schema=False)
def parity_reauth(request: Request):
    """The session-restoring shell `GET /` serves on a dormant Clerk
    session, rendered with the same context."""
    return templates.TemplateResponse(request, "reauth.html", {"user": None})


@router.get("/_parity/sign-out", response_class=HTMLResponse, include_in_schema=False)
def parity_sign_out(request: Request):
    """The interstitial `GET /sign-out` serves under Clerk, rendered
    with the same context."""
    return templates.TemplateResponse(
        request, "sign_out_interstitial.html", {"user": None, "redirect_url": "/"}
    )
