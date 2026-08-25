"""PREP_PARITY_MODE: no page loads a resource from another origin;
without the flag the ClerkJS tag and the vendor doc bundles stay."""

from __future__ import annotations

import base64
import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from prep.auth.port import ResolvedUser, SignInUrls
from prep.auth.providers import set_provider
from prep.web.parity import strip_cross_origin_tags

FRONTEND_HOST = "clerk.example.test"
PUBLISHABLE_KEY = "pk_test_" + base64.b64encode(f"{FRONTEND_HOST}$".encode()).decode()
CLERK_BUNDLE = f"https://{FRONTEND_HOST}/npm/@clerk/clerk-js@5/dist/clerk.browser.js"
REDOC_CDN = "https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js"
SWAGGER_CDN = "cdn.jsdelivr.net/npm/swagger-ui-dist"

_RESOURCE_RE = re.compile(
    r"<(?:script|link)\b[^>]*\b(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)


def _foreign_resources(html: str, host: str = "testserver") -> list[str]:
    return [
        u for u in _RESOURCE_RE.findall(html) if urlsplit(u).netloc and urlsplit(u).netloc != host
    ]


class _VisitorProvider:
    name = "stub"

    def resolve(self, request):
        return None

    def urls(self):
        return SignInUrls(sign_in="/sign-in", sign_out=None, account=None)

    def has_dormant_session(self, request):
        return False


class _MemberProvider(_VisitorProvider):
    """Resolves the test user on every request."""

    def __init__(self, login: str):
        self._login = login

    def resolve(self, request):
        return ResolvedUser(
            external_id=self._login,
            email=self._login,
            display_name="Member",
            profile_pic_url=None,
            provider=self.name,
        )


@pytest.fixture
def visitor_provider():
    set_provider(_VisitorProvider())
    yield
    set_provider(None)


@pytest.fixture
def member_provider(initialized_db):
    set_provider(_MemberProvider(initialized_db))
    yield
    set_provider(None)


@pytest.fixture
def clerk_env(monkeypatch):
    monkeypatch.setenv("PREP_AUTH_MODE", "clerk")
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", PUBLISHABLE_KEY)


@pytest.fixture
def parity_env(monkeypatch):
    monkeypatch.setenv("PREP_PARITY_MODE", "1")


# ---- without the flag ------------------------------------------------------


def test_clerkjs_loads_under_a_publishable_key(clerk_env, visitor_provider, client, initialized_db):
    body = client.get("/").text
    assert CLERK_BUNDLE in body
    assert _foreign_resources(body) == [CLERK_BUNDLE]


def test_doc_shells_load_their_cdn_bundles(client):
    assert SWAGGER_CDN in client.get("/docs").text
    assert REDOC_CDN in client.get("/redoc").text


def test_docs_oauth2_redirect_still_mounted(client):
    assert client.get("/docs/oauth2-redirect").status_code == 200


def test_parity_routes_absent_without_the_flag(client):
    assert client.get("/_parity/raise").status_code == 404


# ---- with the flag ---------------------------------------------------------


def test_landing_references_no_other_origin(
    parity_env, clerk_env, visitor_provider, client, initialized_db
):
    body = client.get("/").text
    assert CLERK_BUNDLE not in body
    assert _foreign_resources(body) == []


def test_dashboard_loads_clerkjs_without_the_flag(clerk_env, member_provider, client):
    body = client.get("/").text
    assert "workflow-badge" in body
    assert _foreign_resources(body) == [CLERK_BUNDLE]


def test_dashboard_references_no_other_origin(parity_env, clerk_env, member_provider, client):
    body = client.get("/").text
    assert "workflow-badge" in body
    assert _foreign_resources(body) == []


def test_doc_shells_reference_no_other_origin(parity_env, client):
    for path in ("/docs", "/redoc"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert _foreign_resources(r.text) == [], path
        assert "<title>" in r.text, path


def test_raise_is_a_deliberate_500(parity_env, client):
    from prep import app as app_mod

    with TestClient(app_mod.app, raise_server_exceptions=False) as c:
        r = c.get("/_parity/raise")
    assert r.status_code == 500
    assert "Something broke" in r.text


def test_boot_warning_names_the_flag(parity_env):
    import logging

    from prep import app as app_mod

    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    log = logging.getLogger("prep")
    handler = _Capture()
    log.addHandler(handler)
    try:
        app_mod._warn_on_parity_mode()
    finally:
        log.removeHandler(handler)
    assert any("PREP_PARITY_MODE" in m for m in messages), messages


# ---- strip_cross_origin_tags -------------------------------------------------

HOST = "prep.example.test"


def test_strip_drops_cross_origin_script():
    html = '<head><script src="https://cdn.example.net/x.js"></script><p>x</p></head>'
    assert strip_cross_origin_tags(html, HOST) == "<head><p>x</p></head>"


def test_strip_keeps_same_host_and_relative_resources():
    html = (
        f'<script src="https://{HOST}/a.js"></script>'
        '<script src="/static/js/app.js" defer></script>'
        '<link rel="stylesheet" href="/static/css/index.css">'
    )
    assert strip_cross_origin_tags(html, HOST) == html


def test_strip_drops_cross_origin_link():
    html = '<link rel="shortcut icon" href="https://fastapi.tiangolo.com/img/favicon.png"><b>k</b>'
    assert strip_cross_origin_tags(html, HOST) == "<b>k</b>"


def test_strip_keeps_inline_scripts():
    html = "<script>const ui = 1;</script>"
    assert strip_cross_origin_tags(html, HOST) == html


def test_strip_compares_hosts_case_insensitively_with_port():
    html = f'<script src="https://{HOST.upper()}:8443/a.js"></script>'
    assert strip_cross_origin_tags(html, f"{HOST}:8443") == html
    assert strip_cross_origin_tags(html, HOST) == ""
