"""The VAPID conversion: the same keypair, in the worker's shape.

The keypair is generated per test rather than committed. A throwaway
P-256 key in a public repo is still key material, and generating it here
also proves the conversion against `py_vapid` itself rather than against
a recorded answer.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01

from migrate.vapid import (
    PRIVATE_SCALAR_BYTES,
    PUBLIC_POINT_BYTES,
    UNCOMPRESSED_POINT_TAG,
    VapidConversionError,
    convert_pem,
    main,
    recorded_public_key,
)


def recorded_b64url(vapid: Vapid01) -> str:
    """The public key in the shape the recorded `vapid-keys.json` holds:
    the uncompressed X9.62 point, base64url, unpadded."""
    raw = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64u(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture
def keypair(tmp_path: Path) -> tuple[Path, Path, str]:
    """A `py_vapid` keypair on disk, in the shape the pre-cutover deploy
    wrote on first boot."""
    vapid = Vapid01()
    vapid.generate_keys()
    pem = tmp_path / "vapid-private.pem"
    keys = tmp_path / "vapid-keys.json"
    pem.write_bytes(vapid.private_pem())
    public = recorded_b64url(vapid)
    keys.write_text(json.dumps({"public_b64": public}, indent=2))
    return pem, keys, public


def test_the_derived_public_key_is_the_recorded_one_byte_for_byte(keypair):
    pem, _keys, public = keypair
    pair = convert_pem(pem.read_bytes())
    assert pair.public_key == public
    raw = unb64u(pair.public_key)
    assert len(raw) == PUBLIC_POINT_BYTES
    assert raw[0] == UNCOMPRESSED_POINT_TAG
    assert len(unb64u(pair.private_key)) == PRIVATE_SCALAR_BYTES


def test_the_conversion_is_a_conversion_not_a_new_keypair(keypair):
    pem, _keys, _public = keypair
    first = convert_pem(pem.read_bytes())
    second = convert_pem(pem.read_bytes())
    assert first == second


def test_the_output_is_the_two_env_vars(keypair, capsys):
    pem, keys, public = keypair
    assert main(["--pem", str(pem), "--keys", str(keys)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == f"PREP_VAPID_PUBLIC_KEY={public}"
    assert lines[1].startswith("PREP_VAPID_PRIVATE_KEY=")


def test_a_live_key_that_differs_aborts_and_names_the_byte(keypair, capsys):
    pem, keys, public = keypair
    wrong = public[:20] + ("A" if public[20] != "A" else "B") + public[21:]
    assert main(["--pem", str(pem), "--keys", str(keys), "--expect", wrong]) == 1
    err = capsys.readouterr().err
    assert "ABORT" in err
    assert "at character 20" in err
    assert "every existing push subscription would go silent" in err


def test_a_matching_live_key_passes(keypair):
    pem, keys, public = keypair
    assert main(["--pem", str(pem), "--keys", str(keys), "--expect", public]) == 0


def test_a_keys_file_that_disagrees_with_the_pem_aborts(keypair, tmp_path, capsys):
    pem, keys, _public = keypair
    other = Vapid01()
    other.generate_keys()
    keys.write_text(json.dumps({"public_b64": recorded_b64url(other)}))
    assert main(["--pem", str(pem), "--keys", str(keys)]) == 1
    assert "vapid-keys.json" in capsys.readouterr().err


def test_a_keys_file_with_no_public_b64_aborts(keypair, capsys):
    pem, keys, _public = keypair
    keys.write_text("{}")
    assert recorded_public_key(keys) is None
    assert main(["--pem", str(pem), "--keys", str(keys)]) == 1
    assert "cannot be checked against the deploy" in capsys.readouterr().err


def test_a_key_on_the_wrong_curve_is_refused(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP384R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(VapidConversionError, match="P-256"):
        convert_pem(pem)
