"""Log-line redaction for AI provider secrets.

Anthropic-issued secrets that prep handles look like:

  - `sk-ant-oat01-<base64ish>`  ← subscription OAuth tokens
                                  (output of `claude setup-token`)
  - `sk-ant-api03-<base64ish>`  ← API keys (BYOK)

Both can end up in logs by accident — a request body echoed in an
exception trace, a debug log that forgot to mask, a third-party
library that logs request headers. None of them should ever land
plaintext on stdout / Loki / Sentry.

The redaction is applied as a `logging.Formatter` wrapper: every log
line our logger emits gets a `re.sub` pass after standard formatting,
replacing matched secrets with `sk-ant-<env>-…REDACTED…`. The first
16 chars are kept so a debugging human can tell which key was in play
without seeing the secret itself.

This is defense in depth, not a primary control — we already keep keys
out of the DB except as ciphertext (see prep/byok/crypto.py) and never
log them deliberately. The filter catches the accidental cases.
"""

from __future__ import annotations

import logging
import re

# Match Anthropic-issued secret shapes. The `[A-Za-z0-9_-]` charset
# covers base64url + the underscore/dash separators Anthropic uses in
# OAuth + API keys. The 20-char minimum suffix avoids hits on a bare
# `sk-ant-api03-` log line and rejects truncated/redacted variants
# we ourselves wrote (e.g. the masked `sk-ant-api03-…x9zT` form has
# the ellipsis, not 20+ chars of base64).
_SECRET_PATTERNS = [
    # Anthropic OAuth tokens + API keys.
    re.compile(r"(sk-ant-(?:api03|oat01)-)[A-Za-z0-9_-]{20,}"),
    # Prep's own personal-access tokens — issued at /settings/api,
    # used as `Authorization: Bearer prep_pat_…` on /api/v1/* and the
    # MCP transport. Same defense-in-depth concern as the Anthropic
    # keys; the lookup hashes at rest but accidental log leaks are
    # how a token would actually escape.
    re.compile(r"(prep_pat_)[A-Za-z0-9_-]{20,}"),
    # OpenAI / OpenRouter shapes — sk- prefix without ant/oat.
    re.compile(r"(sk-or-v1-)[A-Za-z0-9_-]{20,}"),
    re.compile(r"(sk-proj-)[A-Za-z0-9_-]{20,}"),
]
# Kept for backward compat with the old single-pattern API.
_SECRET_PATTERN = _SECRET_PATTERNS[0]


def redact(text: str) -> str:
    """Replace any Anthropic secret in `text` with a safe placeholder.

    Idempotent — running it twice on the same string is a no-op.
    Exposed so call sites that want to scrub a string before stashing
    it somewhere (e.g. an exception message we re-raise) can reach in
    without going through the logging path.
    """
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(r"\1<REDACTED>", out)
    return out


class SecretRedactingFormatter(logging.Formatter):
    """Wraps an inner Formatter and scrubs Anthropic secrets from
    every emitted line.

    Wrap, don't subclass-and-reconfigure, so the existing format
    string + datefmt + style on the inner formatter stay in effect —
    the redaction is purely an output-side transform.
    """

    def __init__(self, inner: logging.Formatter):
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact(self._inner.format(record))

    def formatTime(  # noqa: N802 — match base-class signature
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        return self._inner.formatTime(record, datefmt)

    def formatException(self, ei) -> str:  # noqa: N802
        return redact(self._inner.formatException(ei))

    def formatStack(self, stack_info: str) -> str:  # noqa: N802
        return redact(self._inner.formatStack(stack_info))


def install_on(logger: logging.Logger) -> None:
    """Wrap every handler's formatter on `logger` so its output is
    redacted. Idempotent: calling on an already-wrapped logger leaves
    the existing wrapper alone (we check via `isinstance`)."""
    for handler in logger.handlers:
        existing = handler.formatter
        if isinstance(existing, SecretRedactingFormatter):
            continue
        # If a handler was attached without a formatter, fall back to
        # the logging library default — same behavior the record would
        # have gotten anyway, just wrapped now.
        handler.setFormatter(SecretRedactingFormatter(existing or logging.Formatter()))
