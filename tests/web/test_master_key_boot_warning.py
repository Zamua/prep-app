"""The boot warning for a set-but-unusable master key."""

from __future__ import annotations

import logging


class _Capture(logging.Handler):
    """The `prep` logger sets propagate=False, so caplog never sees it;
    attach directly instead of asserting through the root."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _records_for(fn) -> list[str]:
    log = logging.getLogger("prep")
    handler = _Capture()
    log.addHandler(handler)
    try:
        fn()
    finally:
        log.removeHandler(handler)
    return handler.messages


def test_a_base64_master_key_is_reported_at_boot(monkeypatch):
    """The usual misconfiguration is `openssl rand -base64 32` where the
    loader wants hex. It has to be loud at boot: otherwise the deploy
    looks healthy and BYOK only fails when a user saves a key."""
    from prep import app as app_mod

    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", "CDjk0F" + "A" * 38)
    messages = _records_for(app_mod._warn_on_unusable_master_key)
    assert any("unusable" in m for m in messages), messages


def test_a_hex_master_key_is_silent(monkeypatch):
    from prep import app as app_mod

    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", "ab" * 32)
    assert _records_for(app_mod._warn_on_unusable_master_key) == []


def test_an_unset_master_key_is_silent(monkeypatch):
    """No BYOK configured is a valid deploy shape, not an error."""
    from prep import app as app_mod

    monkeypatch.delenv("PREP_KEY_ENCRYPTION_SECRET", raising=False)
    assert _records_for(app_mod._warn_on_unusable_master_key) == []
