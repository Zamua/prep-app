"""GET / in the provider's "dormant session" state (Clerk handshake:
durable __client_uat present, short-lived __session expired) must
render the session-restoring shell, NOT flash the marketing landing
page at a signed-in user. tests use a stub provider so the branch
logic is exercised without the Clerk SDK."""

import pytest

from prep.auth.port import SignInUrls
from prep.auth.providers import set_provider


class _AnonProvider:
    """Resolves nobody; dormant-session answer is fixed at build time."""

    name = "stub"

    def __init__(self, dormant: bool):
        self._dormant = dormant

    def resolve(self, request):
        return None

    def urls(self):
        return SignInUrls(sign_in="/sign-in", sign_out=None, account=None)

    def has_dormant_session(self, request):
        return self._dormant


@pytest.fixture
def dormant_provider():
    set_provider(_AnonProvider(dormant=True))
    yield
    set_provider(None)


@pytest.fixture
def visitor_provider():
    set_provider(_AnonProvider(dormant=False))
    yield
    set_provider(None)


def test_dormant_session_renders_reauth_shell(client, initialized_db, dormant_provider):
    r = client.get("/")
    assert r.status_code == 200
    assert "Signing you in" in r.text
    # No landing copy: the whole point is not flashing marketing at
    # a signed-in user.
    assert "Start a deck" not in r.text


def test_true_visitor_renders_landing(client, initialized_db, visitor_provider):
    r = client.get("/")
    assert r.status_code == 200
    assert "Start a deck" in r.text
    assert "Signing you in" not in r.text


def test_fallback_cookie_forces_landing(client, initialized_db, dormant_provider):
    """The shell's escape hatch: after a failed recovery it sets
    prep_reauth_fallback=1 and reloads; the server must then serve
    the landing page instead of the shell again (no loop)."""
    client.cookies.set("prep_reauth_fallback", "1")
    r = client.get("/")
    assert r.status_code == 200
    assert "Start a deck" in r.text
    assert "Signing you in" not in r.text
