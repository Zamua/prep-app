"""A push this app encrypts is one a browser can decrypt.

Every unit test for the push crypto runs on Node, whose WebCrypto accepts
key shapes celld's does not. That gap shipped a build where `sent: 1` was
reported for a payload no device could read, so this pins the bytes that
actually leave the worker, against a node running the real runtime.
"""

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import http_ece
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from tests.e2e.celld_node import LocalCelldNode

LOGIN = "push-e2e@example.com"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class _Catcher:
    """Stands in for the push service: keeps the body, answers 201."""

    def __init__(self) -> None:
        captured: list[bytes] = []
        self.captured = captured

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                captured.append(self.rfile.read(int(self.headers.get("content-length", 0))))
                self.send_response(201)
                self.end_headers()

            def log_message(self, *a: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/push"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_Catcher":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="module")
def push_node(celld_build):
    node = LocalCelldNode("push")
    node.start()
    try:
        node.seed_profile(LOGIN, "empty")
        yield node
    finally:
        node.stop()


def test_the_payload_decrypts_with_the_subscription_key(push_node):
    import httpx

    from tests.e2e.celld_node import INTERNAL_TOKEN

    key = ec.generate_private_key(ec.SECP256R1())
    p256dh = key.public_key().public_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.X962,
        format=__import__(
            "cryptography"
        ).hazmat.primitives.serialization.PublicFormat.UncompressedPoint,
    )
    auth = os.urandom(16)
    headers = {"X-Internal-Token": INTERNAL_TOKEN, "tailscale-user-login": LOGIN}

    with _Catcher() as catcher:
        r = httpx.post(
            f"{push_node.base_url}/notify/subscribe",
            json={"endpoint": catcher.url, "keys": {"p256dh": _b64u(p256dh), "auth": _b64u(auth)}},
            headers=headers,
            timeout=30.0,
        )
        assert r.status_code == 200, r.text[:300]

        r = httpx.post(f"{push_node.base_url}/notify/test", headers=headers, timeout=60.0)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["sent"] == 1, r.text[:300]
        assert catcher.captured, "the push never reached the endpoint"

    body = catcher.captured[-1]
    # RFC 8291: the aes128gcm keyid is the sender's uncompressed P-256 point.
    # celld's exportKey('raw') answers SPKI DER, which encrypts fine and
    # decrypts nowhere.
    id_len = body[20]
    assert id_len == 65, f"keyid is {id_len} bytes, not an uncompressed point"
    assert body[21] == 0x04, f"keyid starts {body[21]:#x}, not 0x04"

    plaintext = http_ece.decrypt(body, private_key=key, auth_secret=auth)
    assert json.loads(plaintext)["title"]
