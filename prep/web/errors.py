"""Friendly error pages + JSON-aware exception handlers.

FastAPI's default for any HTTPException is `{"detail": "..."}` JSON,
which renders raw in a browser tab. For HTML clients we replace that
with a literary-styled error page that explains what happened and
offers a way back home. JSON clients (anyone who sets
`Accept: application/json` or hits a /notify/* endpoint that returns
JSON-shaped data) keep the bare detail response so callers can parse
the error programmatically.
"""

from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from prep.web.templates import templates

_ERROR_COPY = {
    400: ("Bad request.", "Something in that URL didn't quite parse."),
    401: (
        "Not signed in.",
        "prep authenticates via Tailscale Serve — open this page through your tailnet so the server can read your Tailscale identity. "
        "For local development, set PREP_DEFAULT_USER (the make dev shim does this automatically).",
    ),
    403: ("Forbidden.", "That's not yours to look at."),
    404: (
        "Not found.",
        "We couldn't find what you were looking for. Maybe a typo, or the link is stale.",
    ),
    409: ("Out of date.", "Something changed since this page loaded. Reload and try again."),
    422: ("Bad input.", "The form didn't validate. Go back and try again."),
    500: ("Something broke.", "Sorry — that's on our end. The error has been logged."),
}


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    # Any /notify/* JSON endpoint should not get an HTML error page on its
    # POST responses — the JS code on the demo / settings page expects JSON.
    path = request.url.path
    if (
        path.endswith("/subscribe")
        or path.endswith("/unsubscribe")
        or path.endswith("/test")
        or path.endswith("/prefs")
        or path.endswith("/vapid-public-key")
    ):
        return True
    return False


def _render_error(request: Request, status_code: int, detail: str | None = None):
    headline, blurb = _ERROR_COPY.get(
        status_code,
        ("Something went sideways.", "An unexpected error happened. The team has been notified."),
    )
    if detail and detail != headline:
        # Fold the original detail into the blurb so we don't lose
        # context (e.g., "malformed workflow id" vs. generic "Bad request").
        blurb = f"{blurb} ({detail})"
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "status_code": status_code,
            "headline": headline,
            "blurb": blurb,
            "path": request.url.path,
        },
        status_code=status_code,
    )


def register(app: FastAPI) -> None:
    """Wire the three exception handlers onto the given FastAPI app."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if _wants_json(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return _render_error(request, exc.status_code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        if _wants_json(request):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        return _render_error(request, 422, "Form did not validate.")

    @app.exception_handler(Exception)
    async def server_exception_handler(request: Request, exc: Exception):
        logging.getLogger("prep").error(
            "unhandled exception on %s %s: %s\n%s",
            request.method,
            request.url.path,
            exc,
            traceback.format_exc(),
        )
        if _wants_json(request):
            return JSONResponse({"detail": "internal server error"}, status_code=500)
        return _render_error(request, 500)
