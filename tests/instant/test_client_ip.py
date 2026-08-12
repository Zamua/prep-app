"""client_ip(): the limiter-bucket derivation.

Pins the fail-closed rules: only the configured header is trusted,
anything unparseable lands in the shared sentinel bucket, and
`request.client.host` is never consulted (it is client-forgeable
under the deploy's trust-all proxy-header config).
"""

from __future__ import annotations

import pytest
from fastapi import Request

from prep.instant.routes import SENTINEL_BUCKET, client_ip


def _req(headers: dict[str, str], client_host: str = "203.0.113.50") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/instant/generate",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 4242),
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


# ---- default mode (x-real-ip) ---------------------------------------------


def test_default_mode_reads_x_real_ip():
    assert client_ip(_req({"x-real-ip": "198.51.100.7"})) == "198.51.100.7"


def test_missing_header_is_sentinel_never_client_host():
    # The socket peer is a perfectly valid IP; it must still not be used.
    assert client_ip(_req({}, client_host="198.51.100.9")) == SENTINEL_BUCKET


@pytest.mark.parametrize(
    "value",
    ["not-an-ip", "1.2.3.4, 5.6.7.8", "1.2.3.4:443", "", "  ", "example.com"],
)
def test_garbage_header_is_sentinel(value: str):
    assert client_ip(_req({"x-real-ip": value}, client_host="198.51.100.9")) == SENTINEL_BUCKET


def test_xff_is_ignored_in_default_mode():
    # A forged X-Forwarded-For with no ingress-set x-real-ip cannot
    # choose its own bucket.
    assert client_ip(_req({"x-forwarded-for": "9.9.9.9"})) == SENTINEL_BUCKET


def test_spoofed_xff_does_not_move_the_derived_ip():
    headers = {"x-real-ip": "198.51.100.7", "x-forwarded-for": "9.9.9.9"}
    assert client_ip(_req(headers)) == "198.51.100.7"


# ---- x-forwarded-for-last mode ---------------------------------------------


def test_xff_last_mode_takes_the_last_entry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PREP_CLIENT_IP_HEADER", "x-forwarded-for-last")
    headers = {"x-forwarded-for": "9.9.9.9, 203.0.113.1, 192.0.2.3"}
    assert client_ip(_req(headers)) == "192.0.2.3"


def test_xff_last_mode_single_entry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PREP_CLIENT_IP_HEADER", "x-forwarded-for-last")
    assert client_ip(_req({"x-forwarded-for": "192.0.2.3"})) == "192.0.2.3"


def test_xff_last_mode_missing_header_is_sentinel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PREP_CLIENT_IP_HEADER", "x-forwarded-for-last")
    assert client_ip(_req({}, client_host="198.51.100.9")) == SENTINEL_BUCKET


def test_custom_header_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PREP_CLIENT_IP_HEADER", "CF-Connecting-IP")
    assert client_ip(_req({"cf-connecting-ip": "192.0.2.7"})) == "192.0.2.7"


# ---- normalization ----------------------------------------------------------


def test_ipv4_keys_on_the_exact_address():
    assert client_ip(_req({"x-real-ip": "192.0.2.7"})) == "192.0.2.7"
    assert client_ip(_req({"x-real-ip": "192.0.2.8"})) == "192.0.2.8"


def test_ipv6_same_slash64_share_one_bucket():
    a = client_ip(_req({"x-real-ip": "2001:db8:aa:bb::1"}))
    b = client_ip(_req({"x-real-ip": "2001:db8:aa:bb:dead:beef:1234:5678"}))
    assert a == b == "2001:db8:aa:bb::/64"


def test_ipv6_different_slash64_do_not_share():
    a = client_ip(_req({"x-real-ip": "2001:db8:aa:bb::1"}))
    b = client_ip(_req({"x-real-ip": "2001:db8:aa:cc::1"}))
    assert a != b


def test_ipv4_mapped_ipv6_normalizes_to_the_v4_bucket():
    mapped = client_ip(_req({"x-real-ip": "::ffff:198.51.100.9"}))
    plain = client_ip(_req({"x-real-ip": "198.51.100.9"}))
    assert mapped == plain == "198.51.100.9"
