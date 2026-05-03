"""send_to_user must prepend ROOT_PATH to the tap-target URL.

The service worker treats data.url as origin-absolute. Without the
prefix, the URL lands outside the PWA scope on iOS — the browser
falls back to start_url and the user ends up on the landing page
instead of the card. Hit on prod v0.15.0; this test pins the fix.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def patched_send(monkeypatch, env):
    """Drop ROOT_PATH into env, stub _send_one so we capture payloads
    instead of trying to hit Apple's push gateway."""
    monkeypatch.setenv("ROOT_PATH", "/prep")
    import importlib

    from prep.notify import push as _push_mod

    importlib.reload(_push_mod)

    captured: list[dict] = []

    def fake_send_one(_sub_row, payload):
        captured.append(payload)
        return "ok"

    monkeypatch.setattr(_push_mod, "_send_one", fake_send_one)
    monkeypatch.setattr(_push_mod.db, "list_push_subscriptions", lambda _u: [{"endpoint": "x"}])
    return _push_mod, captured


def test_send_to_user_prepends_root_path_for_app_relative_url(patched_send):
    mod, captured = patched_send
    mod.send_to_user("u", "t", "b", url="/trivia/session/foo")
    assert captured[0]["url"] == "/prep/trivia/session/foo"


def test_send_to_user_does_not_double_prefix(patched_send):
    """If a caller already prefixed the URL, don't add another."""
    mod, captured = patched_send
    mod.send_to_user("u", "t", "b", url="/prep/trivia/session/foo")
    assert captured[0]["url"] == "/prep/trivia/session/foo"


def test_send_to_user_no_root_path_passthrough(monkeypatch, env):
    """Dev / native install with empty ROOT_PATH should leave URL alone."""
    monkeypatch.setenv("ROOT_PATH", "")
    import importlib

    from prep.notify import push as _push_mod

    importlib.reload(_push_mod)
    captured: list[dict] = []
    monkeypatch.setattr(_push_mod, "_send_one", lambda _s, p: captured.append(p) or "ok")
    monkeypatch.setattr(_push_mod.db, "list_push_subscriptions", lambda _u: [{"endpoint": "x"}])
    _push_mod.send_to_user("u", "t", "b", url="/trivia/foo")
    assert captured[0]["url"] == "/trivia/foo"


def test_send_to_user_default_url_root(patched_send):
    """Empty url defaults to "/" — should also pick up the prefix."""
    mod, captured = patched_send
    mod.send_to_user("u", "t", "b")
    assert captured[0]["url"] == "/prep/"


def test_send_to_user_forwards_tag_to_payload(patched_send):
    """`tag` flows into the SW payload so iOS can coalesce stacked
    notifications. Default (no tag) leaves the key out → SW falls
    back to "prep-default"."""
    mod, captured = patched_send
    mod.send_to_user("u", "t", "b", tag="trivia-design-interview")
    assert captured[0]["tag"] == "trivia-design-interview"

    captured.clear()
    mod.send_to_user("u", "t", "b")  # no tag passed
    assert "tag" not in captured[0]
