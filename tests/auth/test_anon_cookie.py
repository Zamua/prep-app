"""The `prep_anon` cookie codec: signing, expiry, the rolling window
and secret resolution."""

from __future__ import annotations

import pytest

from prep.auth import anon_cookie as ac

DAY = 86400
EXTERNAL_ID = "anon:" + "ab" * 16
OTHER_ID = "anon:" + "cd" * 16
MASTER = "11" * 32
EXPLICIT = "22" * 32


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)


@pytest.fixture
def no_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.delenv(ac.MASTER_ENV, raising=False)


def test_round_trip(secret):
    value = ac.mint_cookie(EXTERNAL_ID, issued_at=1000)
    parsed = ac.verify_cookie(value, now=1000)
    assert parsed == ac.AnonCookie(external_id=EXTERNAL_ID, issued_at=1000)


def test_value_shape(secret):
    value = ac.mint_cookie(EXTERNAL_ID, issued_at=1000)
    version, ident, iat, sig = value.split(".")
    assert version == "v1"
    assert iat == "1000"
    # 16 bytes of id and 16 bytes of signature, unpadded base64url.
    assert len(ident) == 22 and "=" not in ident
    assert len(sig) == 22 and "=" not in sig


def test_tampered_signature_rejected(secret):
    value = ac.mint_cookie(EXTERNAL_ID, issued_at=1000)
    head, sig = value.rsplit(".", 1)
    flipped = ("B" if sig[0] != "B" else "C") + sig[1:]
    assert ac.verify_cookie(f"{head}.{flipped}", now=1000) is None


def test_tampered_id_rejected(secret):
    value = ac.mint_cookie(EXTERNAL_ID, issued_at=1000)
    other = ac.mint_cookie(OTHER_ID, issued_at=1000)
    forged = ".".join([value.split(".")[0], other.split(".")[1], *value.split(".")[2:]])
    assert ac.verify_cookie(forged, now=1000) is None


def test_tampered_iat_rejected(secret):
    value = ac.mint_cookie(EXTERNAL_ID, issued_at=1000)
    version, ident, _, sig = value.split(".")
    assert ac.verify_cookie(f"{version}.{ident}.999999.{sig}", now=1000) is None


def test_future_iat_rejected(secret):
    value = ac.mint_cookie(EXTERNAL_ID, issued_at=10_000 + 120)
    assert ac.verify_cookie(value, now=10_000) is None
    # Inside the skew allowance it still verifies.
    assert ac.verify_cookie(ac.mint_cookie(EXTERNAL_ID, issued_at=10_030), now=10_000)


def test_expired_iat_rejected(secret):
    issued = 1_000_000
    assert ac.verify_cookie(ac.mint_cookie(EXTERNAL_ID, issued_at=issued), now=issued + 179 * DAY)
    assert (
        ac.verify_cookie(ac.mint_cookie(EXTERNAL_ID, issued_at=issued), now=issued + 181 * DAY)
        is None
    )


def test_wrong_secret_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)
    value = ac.mint_cookie(EXTERNAL_ID, issued_at=1000)
    monkeypatch.setenv(ac.MASTER_ENV, "33" * 32)
    assert ac.verify_cookie(value, now=1000) is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "garbage",
        "v1.only.three",
        "v1.a.b.c.d",
        "v2.q6urq6urq6urq6urq6urqw.1000.sig",
        "v1.!!!.1000.sig",
        "v1.q6urq6urq6urq6urq6urqw.notanint.sig",
        "v1.c2hvcnQ.1000.sig",
        # Non-ASCII in each field: comparing or signing these raises
        # rather than returning, so they must be refused before either.
        "v1.q6urq6urq6urq6urq6urqw.1000.\xe9AAAAAAAAAAAAAAAAAAAAA",
        "v1.q6urq6urq6urq6urq6urq\xe9.1000.sig",
        "v1.q6urq6urq6urq6urq6urqw.10\xe900.sig",
    ],
)
def test_garbage_rejected_without_raising(secret, raw):
    assert ac.verify_cookie(raw, now=1000) is None


def test_none_is_absent(secret):
    assert ac.verify_cookie(None) is None


def test_refresh_threshold(secret):
    issued = 1_000_000
    cookie = ac.AnonCookie(external_id=EXTERNAL_ID, issued_at=issued)
    assert ac.needs_refresh(cookie, now=issued + 31 * DAY) is True
    assert ac.needs_refresh(cookie, now=issued + 29 * DAY) is False


def test_remint_keeps_id_and_survives_to_day_200(secret):
    """The regression the rolling window exists to prevent: a value
    re-minted at day 31 still verifies at day 200, where a Max-Age
    refresh of the original value would already be dead."""
    issued = 1_000_000
    original = ac.mint_cookie(EXTERNAL_ID, issued_at=issued)
    day31 = issued + 31 * DAY
    parsed = ac.verify_cookie(original, now=day31)
    assert parsed is not None and ac.needs_refresh(parsed, now=day31)

    reminted = ac.mint_cookie(parsed.external_id, issued_at=day31)
    assert reminted != original

    day200 = issued + 200 * DAY
    assert ac.verify_cookie(original, now=day200) is None
    still_good = ac.verify_cookie(reminted, now=day200)
    assert still_good is not None
    assert still_good.external_id == EXTERNAL_ID
    assert still_good.issued_at == day31


def test_explicit_secret_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)
    monkeypatch.setenv(ac.SECRET_ENV, EXPLICIT)
    assert ac.cookie_secret() == bytes.fromhex(EXPLICIT)


def test_hkdf_fallback_is_deterministic_and_not_the_master(secret):
    derived = ac.cookie_secret()
    assert derived == ac.cookie_secret()
    assert derived is not None
    assert len(derived) == 32
    assert derived != bytes.fromhex(MASTER)


def test_hkdf_fallback_differs_per_master(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)
    first = ac.cookie_secret()
    monkeypatch.setenv(ac.MASTER_ENV, "44" * 32)
    assert ac.cookie_secret() != first


def test_malformed_explicit_secret_fails_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)
    monkeypatch.setenv(ac.SECRET_ENV, "not-hex")
    assert ac.cookie_secret() is None
    assert ac.is_enabled() is False


def test_no_secret_disables_anonymous_accounts(no_secret):
    assert ac.cookie_secret() is None
    assert ac.is_enabled() is False
    assert ac.verify_cookie("v1.q6urq6urq6urq6urq6urqw.1000.sig") is None
    with pytest.raises(ac.AnonCookieDisabled):
        ac.mint_cookie(EXTERNAL_ID)


def test_mint_rejects_a_non_anonymous_external_id(secret):
    with pytest.raises(ValueError):
        ac.mint_cookie("user_2abc")
    with pytest.raises(ValueError):
        ac.mint_cookie("anon:ff")
