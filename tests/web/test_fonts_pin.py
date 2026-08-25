"""The self-hosted fonts pin: no page references a Google font host,
every face in fonts.css is a committed file the app serves, and the
families match the token stacks."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

import pytest

from prep.auth.port import SignInUrls
from prep.auth.providers import set_provider
from prep.web.templates import get_build_token

REPO = Path(__file__).resolve().parent.parent.parent
FONTS_CSS = REPO / "static" / "css" / "fonts.css"
TOKENS_CSS = REPO / "static" / "css" / "tokens.css"
GOOGLE_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")

_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")
_FAMILY_RE = re.compile(r"font-family:\s*['\"]([^'\"]+)['\"]")


class _VisitorProvider:
    name = "stub"

    def resolve(self, request):
        return None

    def urls(self):
        return SignInUrls(sign_in="/sign-in", sign_out=None, account=None)

    def has_dormant_session(self, request):
        return False


@pytest.fixture
def visitor_provider():
    set_provider(_VisitorProvider())
    yield
    set_provider(None)


def _font_urls() -> list[str]:
    return _URL_RE.findall(FONTS_CSS.read_text())


def test_no_page_references_a_google_font_host(client, initialized_db):
    for path in ("/", "/privacy"):
        body = client.get(path).text
        for host in GOOGLE_HOSTS:
            assert host not in body, (path, host)
        assert "/fonts.css" in body, path


def test_landing_references_no_google_font_host(client, initialized_db, visitor_provider):
    body = client.get("/").text
    assert "sign-in" in body
    for host in GOOGLE_HOSTS:
        assert host not in body
    assert "/fonts.css" in body


def test_every_face_is_a_committed_file():
    """URLs resolve against the served path, /static/css/v<token>/fonts.css."""
    urls = _font_urls()
    assert urls, "fonts.css declares no faces"
    served = f"/static/css/v{get_build_token()}/fonts.css"
    for rel in urls:
        path = urljoin(served, rel)
        assert path.startswith("/static/fonts/"), rel
        assert (REPO / path.lstrip("/")).is_file(), rel


def test_families_match_the_token_stacks():
    families = set(_FAMILY_RE.findall(FONTS_CSS.read_text()))
    tokens = TOKENS_CSS.read_text()
    stacks = {
        re.search(rf"--{name}:\s*\"([^\"]+)\"", tokens).group(1) for name in ("serif", "mono")
    }
    assert families == stacks


def test_fonts_css_names_no_remote_host():
    assert not [u for u in _font_urls() if "://" in u]


def test_each_font_is_served_as_woff2(client):
    css_path = f"/static/css/v{get_build_token()}/fonts.css"
    r = client.get(css_path)
    assert r.status_code == 200, css_path
    assert r.headers["content-type"].startswith("text/css")
    urls = _URL_RE.findall(r.text)
    assert urls
    for rel in urls:
        path = urljoin(css_path, rel)
        assert path.startswith("/static/fonts/"), path
        font = client.get(path)
        assert font.status_code == 200, path
        assert font.headers["content-type"] == "font/woff2", path
        assert font.content[:4] == b"wOF2", path


def test_fonts_stay_out_of_the_precache(client):
    body = client.get("/sw.js").text
    assert "/static/fonts/" not in body


def test_dockerfile_ships_the_font_files():
    """The image copies static/ by subdirectory, so a new subdirectory
    is invisible to prod until named here. fonts.css points at
    static/fonts; the image must carry it."""
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.prep").read_text()
    assert "COPY static/fonts/ ./static/fonts/" in dockerfile
