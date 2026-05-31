"""Crypto primitive tests — round-trip, tamper detection, master key
parsing, masking. Pure unit tests, no DB / no I/O."""

from __future__ import annotations

import base64

import pytest

from prep.byok import crypto

# A deterministic 32-byte hex key the tests can use. Production
# always uses `openssl rand -hex 32` per the docstring.
_TEST_KEY_HEX = "00" * 32
_TEST_KEY = bytes.fromhex(_TEST_KEY_HEX)


def test_encrypt_decrypt_roundtrip():
    plain = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    blob = crypto.encrypt(plain, _TEST_KEY)
    # The blob is opaque ASCII; the secret is nowhere in it.
    assert plain not in blob
    assert "sk-ant" not in blob
    assert crypto.decrypt(blob, _TEST_KEY) == plain


def test_encrypt_uses_fresh_nonce_per_call():
    """Two encryptions of the same plaintext + same key must produce
    different ciphertext — otherwise we've leaked equality between
    rows (an attacker with DB access could tell two users supplied
    the same key)."""
    plain = "sk-ant-api03-zzz"
    a = crypto.encrypt(plain, _TEST_KEY)
    b = crypto.encrypt(plain, _TEST_KEY)
    assert a != b
    # Both still decrypt to the same plaintext though.
    assert crypto.decrypt(a, _TEST_KEY) == plain
    assert crypto.decrypt(b, _TEST_KEY) == plain


def test_decrypt_detects_tag_tamper():
    """Flip a bit in the tag region — AES-GCM must refuse."""
    blob = crypto.encrypt("sk-ant-api03-some-key", _TEST_KEY)
    raw = bytearray(base64.b64decode(blob))
    raw[-1] ^= 0x01
    tampered = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(tampered, _TEST_KEY)


def test_decrypt_detects_ciphertext_tamper():
    blob = crypto.encrypt("sk-ant-api03-some-key", _TEST_KEY)
    raw = bytearray(base64.b64decode(blob))
    # Flip a bit inside the ciphertext body (skip the 12-byte nonce
    # so we're not just changing which nonce is being used).
    raw[crypto.NONCE_LEN] ^= 0x01
    tampered = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(tampered, _TEST_KEY)


def test_decrypt_with_wrong_key_fails():
    blob = crypto.encrypt("sk-ant-api03-key", _TEST_KEY)
    other = bytes.fromhex("ff" * 32)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob, other)


def test_decrypt_rejects_malformed_base64():
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt("not-base64!!!", _TEST_KEY)


def test_decrypt_rejects_truncated_blob():
    short = base64.b64encode(b"\x00" * 5).decode("ascii")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(short, _TEST_KEY)


def test_encrypt_rejects_empty():
    with pytest.raises(ValueError):
        crypto.encrypt("", _TEST_KEY)


def test_load_master_from_env_happy_path(monkeypatch):
    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", _TEST_KEY_HEX)
    k = crypto.load_master_from_env()
    assert k == _TEST_KEY


def test_load_master_from_env_missing(monkeypatch):
    monkeypatch.delenv("PREP_KEY_ENCRYPTION_SECRET", raising=False)
    with pytest.raises(crypto.MasterKeyError):
        crypto.load_master_from_env()


def test_load_master_from_env_not_hex(monkeypatch):
    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", "not-hex-at-all")
    with pytest.raises(crypto.MasterKeyError):
        crypto.load_master_from_env()


def test_load_master_from_env_wrong_length(monkeypatch):
    # 16 bytes, not 32 — too short for AES-256.
    monkeypatch.setenv("PREP_KEY_ENCRYPTION_SECRET", "00" * 16)
    with pytest.raises(crypto.MasterKeyError):
        crypto.load_master_from_env()


def test_mask_default_shape():
    out = crypto.mask("sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123x9zT")
    # 14-char prefix, ellipsis, 4-char suffix.
    assert out.startswith("sk-ant-api03-a")
    assert out.endswith("x9zT")
    assert "…" in out
    assert "abcdefghijklmnop" not in out


def test_mask_short_input_falls_back_to_placeholder():
    # Anything that would leak more than masking allows returns a
    # safe placeholder instead.
    assert crypto.mask("short") == "…"
    assert crypto.mask("") == "…"
