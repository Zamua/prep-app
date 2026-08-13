"""Helpers for the three audiences an anonymous deploy serves: a
signed-out visitor, an anonymous account, and a signed-in user.

The fixtures built on these live in tests/conftest.py; this module
holds only what a test imports by name.
"""

from __future__ import annotations

from prep.auth.port import ResolvedUser, SignInUrls
from prep.infrastructure.db import cursor

ANON_ID = "anon:" + "ab" * 16
SIGNED_IN_ID = "user_2abc"
MASTER_KEY = "11" * 32

SIGNED_IN = ResolvedUser(
    external_id=SIGNED_IN_ID,
    email="alice@example.com",
    display_name="Alice",
    profile_pic_url=None,
    provider="stub",
)


class Inner:
    """Stand-in for the provider adapter the anonymous fallback wraps."""

    name = "stub"

    def __init__(self, *, user: ResolvedUser | None = None, dormant: bool = False):
        self._user = user
        self._dormant = dormant

    def resolve(self, request):
        return self._user

    def urls(self):
        return SignInUrls(sign_in="/sign-in-here", sign_out="/sign-out-here", account=None)

    def has_dormant_session(self, request):
        return self._dormant


def seed_anon_user(external_id: str = ANON_ID) -> str:
    with cursor() as c:
        c.execute(
            """INSERT INTO users (tailscale_login, display_name, email, created_at,
                                  last_seen_at, is_anonymous)
               VALUES (?, 'Guest', NULL, ?, ?, 1)""",
            (external_id, "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"),
        )
    return external_id


def seed_named_user(external_id: str = SIGNED_IN_ID) -> str:
    from prep.auth.repo import UserRepo

    UserRepo().upsert(external_id, display_name="Alice")
    return external_id
