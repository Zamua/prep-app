"""User repository for the auth context.

Wraps the user-related accessors in prep.db with a focused surface —
profile + editor preference today; future user-scoped settings land
here too.
"""

from __future__ import annotations

from prep import db as _legacy_db


class UserRepo:
    """Read/write access to the users table."""

    def upsert(
        self,
        tailscale_login: str,
        display_name: str | None = None,
        profile_pic_url: str | None = None,
    ) -> dict:
        """Create-or-update a user row from the Tailscale identity
        headers. Returns the canonical user dict."""
        return _legacy_db.upsert_user(tailscale_login, display_name, profile_pic_url)

    def get_editor_input_mode(self, user_id: str) -> str:
        return _legacy_db.get_editor_input_mode(user_id)

    def set_editor_input_mode(self, user_id: str, mode: str) -> None:
        _legacy_db.set_editor_input_mode(user_id, mode)

    @property
    def editor_input_modes(self) -> tuple[str, ...]:
        """The set of legal `mode` values for the editor settings form."""
        return tuple(_legacy_db.EDITOR_INPUT_MODES)
