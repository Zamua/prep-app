"""prep.agent.usage — token-scoped usage tracking.

Each agent invocation records one row in `agent_usage`. Rollups are
**per token**, not per user, because the OAuth token (from
`claude setup-token`) is what Anthropic bills against — multiple
prep users could share one token, and the credit pool is a
property of the token.

`token_hash` is sha256 of the OAuth token value, never the token
itself, so the rollup table is safe to dump in logs / share for
debugging. The hash is stable across restarts so monthly rollups
keep accumulating to the same row.

`user_login` is kept as a secondary dimension so a future "who is
using my credits" breakdown is possible, but the canonical
"remaining $X" answer is `SUM(cost_usd) WHERE token_hash = ?`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from prep.infrastructure.db import cursor


def hash_token(token: str) -> str:
    """sha256 of the OAuth token, hex-encoded. Use this as the key in
    the usage table so the real token never appears in dumps."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentUsage:
    """One agent invocation's accounting row."""

    token_hash: str
    called_at: str  # ISO 8601 UTC
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    user_login: str | None  # secondary dimension, optional


class AgentUsageRepo:
    """Persistence for `agent_usage`. One responsibility: append a row
    per call + serve the rollup. No service logic."""

    def record(
        self,
        *,
        token_hash: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
        user_login: str | None = None,
    ) -> None:
        """Append a single usage row. Idempotency is not enforced —
        each invocation is its own row."""
        with cursor() as c:
            c.execute(
                """
                INSERT INTO agent_usage
                    (token_hash, called_at, model,
                     input_tokens, output_tokens, cost_usd, user_login)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    model,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    user_login,
                ),
            )

    def monthly_cost(self, token_hash: str, *, month_start_iso: str) -> float:
        """Sum cost_usd for this token from `month_start_iso` (inclusive)
        to now. Returns 0.0 if no calls recorded. Caller supplies the
        month boundary (so 'monthly' is what the caller calls it —
        calendar month, last 30 days, whatever)."""
        with cursor() as c:
            row = c.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0.0) AS total
                  FROM agent_usage
                 WHERE token_hash = ? AND called_at >= ?
                """,
                (token_hash, month_start_iso),
            ).fetchone()
        return float(row["total"] or 0.0)

    def call_count(self, token_hash: str, *, month_start_iso: str) -> int:
        """Number of calls for this token since `month_start_iso`. Cheap
        signal for the settings page ('132 calls this month')."""
        with cursor() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS n
                  FROM agent_usage
                 WHERE token_hash = ? AND called_at >= ?
                """,
                (token_hash, month_start_iso),
            ).fetchone()
        return int(row["n"] or 0)
