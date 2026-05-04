"""User repository for the auth context.

Owns persistence for the `users` table — profile (Tailscale identity),
editor preference, notification preferences. SQL lives here directly;
no wrapping over prep.db.
"""

from __future__ import annotations

import json

from prep.infrastructure.db import cursor, now

# Editor input mode is a single user setting (CodeMirror keybinding
# extension). Values are validated at the boundary; routes coerce to
# the default if the column is NULL or carries an unknown legacy value.
EDITOR_INPUT_MODES = ("vanilla", "vim", "emacs")
DEFAULT_EDITOR_INPUT_MODE = "vanilla"


# Default prefs for a fresh user — explicit opt-in, so mode starts off.
# JSON-merged on every read so callers always see every key, even for
# users who've never opened settings.
DEFAULT_NOTIFICATION_PREFS = {
    "mode": "off",  # off | digest | when-ready
    "digest_hour": 9,  # 0..23 local-tz hour for digest mode
    "tz": "America/New_York",  # IANA timezone name
    "threshold": 3,  # min due cards for when-ready mode
    "quiet_hours_enabled": False,  # opt-in; when false, no quiet window
    "quiet_start_hour": 22,  # 0..23, only honored when enabled
    "quiet_end_hour": 8,
    # State (not user-edited; updated by the scheduler):
    "last_digest_date": None,  # ISO date "YYYY-MM-DD" in user tz
    "last_when_ready_at": None,  # ISO datetime UTC, debounce window
}


class UserRepo:
    """Read/write access to the users table."""

    def upsert(
        self,
        tailscale_login: str,
        display_name: str | None = None,
        profile_pic_url: str | None = None,
    ) -> dict:
        """Called on every authenticated request. Upserts the user row
        and bumps last_seen_at. Returns the canonical user dict."""
        ts = now()
        with cursor() as c:
            c.execute(
                """INSERT INTO users (tailscale_login, display_name, profile_pic_url, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tailscale_login) DO UPDATE SET
                     display_name = COALESCE(?, users.display_name),
                     profile_pic_url = COALESCE(?, users.profile_pic_url),
                     last_seen_at = ?""",
                (
                    tailscale_login,
                    display_name,
                    profile_pic_url,
                    ts,
                    ts,
                    display_name,
                    profile_pic_url,
                    ts,
                ),
            )
            return dict(
                c.execute(
                    "SELECT * FROM users WHERE tailscale_login = ?", (tailscale_login,)
                ).fetchone()
            )

    def get_editor_input_mode(self, user_id: str) -> str:
        """Returns the user's preferred CodeMirror input mode. Falls
        back to DEFAULT_EDITOR_INPUT_MODE if the column is NULL or
        unrecognised."""
        with cursor() as c:
            row = c.execute(
                "SELECT editor_input_mode FROM users WHERE tailscale_login = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return DEFAULT_EDITOR_INPUT_MODE
        val = row["editor_input_mode"]
        if val in EDITOR_INPUT_MODES:
            return val
        return DEFAULT_EDITOR_INPUT_MODE

    def set_editor_input_mode(self, user_id: str, mode: str) -> None:
        if mode not in EDITOR_INPUT_MODES:
            raise ValueError(f"unknown editor input mode {mode!r}")
        with cursor() as c:
            c.execute(
                "UPDATE users SET editor_input_mode = ? WHERE tailscale_login = ?",
                (mode, user_id),
            )

    @property
    def editor_input_modes(self) -> tuple[str, ...]:
        """The set of legal `mode` values for the editor settings form."""
        return EDITOR_INPUT_MODES

    def get_notification_prefs(self, user_id: str) -> dict:
        """Return current prefs merged over defaults so callers always
        see every key. Defaults apply for users who've never opened
        settings."""
        with cursor() as c:
            row = c.execute(
                "SELECT notification_prefs FROM users WHERE tailscale_login = ?",
                (user_id,),
            ).fetchone()
        raw = row["notification_prefs"] if row and row["notification_prefs"] else None
        saved = json.loads(raw) if raw else {}
        return {**DEFAULT_NOTIFICATION_PREFS, **saved}

    def set_notification_prefs(self, user_id: str, prefs: dict) -> None:
        """Persist prefs. Caller is responsible for validation (we
        trust the settings route to clamp ranges and validate the
        mode enum)."""
        with cursor() as c:
            c.execute(
                "UPDATE users SET notification_prefs = ? WHERE tailscale_login = ?",
                (json.dumps(prefs), user_id),
            )
