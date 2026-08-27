"""VAPID: the same keypair, in the shape the worker reads.

Python holds a P-256 private key as PKCS8 PEM (`vapid-private.pem`) plus
the uncompressed public point base64url-unpadded in `vapid-keys.json`.
The worker wants two base64url strings: `PREP_VAPID_PUBLIC_KEY`, the
65-byte uncompressed point, and `PREP_VAPID_PRIVATE_KEY`, the 32-byte
scalar.

This is a **format conversion of the same keypair**, never a new one.
Existing `push_subscriptions` survive because a subscription is bound to
the application server key the browser subscribed with: the push service
accepts a VAPID JWT signed by the matching private key, and the bytes are
unchanged. RFC 8291 encryption uses the subscription's own p256dh/auth,
which the migration does not touch.

Mint a fresh keypair instead and every migrated subscription goes silent:
push services answer 403 to a JWT signed by a key that does not match the
`applicationServerKey` the subscription carries, and nothing in the UI
says so. `--expect` exists so that failure is a gate rather than a thing
noticed weeks later.

    python -m prep.migrate.vapid --pem <path> --keys <path> [--expect <b64url>]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

PUBLIC_POINT_BYTES = 65
PRIVATE_SCALAR_BYTES = 32
UNCOMPRESSED_POINT_TAG = 0x04


class VapidConversionError(RuntimeError):
    """The keypair is not the one the deploy is using, or is not P-256."""


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class VapidPair:
    public_key: str
    private_key: str

    def env_lines(self) -> str:
        return f"PREP_VAPID_PUBLIC_KEY={self.public_key}\nPREP_VAPID_PRIVATE_KEY={self.private_key}"


def convert_pem(pem: bytes) -> VapidPair:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise VapidConversionError(
            "the PEM does not hold a P-256 private key, which is the only curve VAPID allows"
        )
    point = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    if len(point) != PUBLIC_POINT_BYTES or point[0] != UNCOMPRESSED_POINT_TAG:
        raise VapidConversionError(
            f"the public point is {len(point)} bytes, not an uncompressed {PUBLIC_POINT_BYTES}"
        )
    scalar = key.private_numbers().private_value.to_bytes(PRIVATE_SCALAR_BYTES, "big")
    return VapidPair(public_key=b64u(point), private_key=b64u(scalar))


def recorded_public_key(keys_path: Path) -> str | None:
    """`vapid-keys.json`'s `public_b64`: what the Python app has been
    handing browsers, and therefore what every existing subscription is
    bound to."""
    if not keys_path.is_file():
        return None
    meta = json.loads(keys_path.read_text(encoding="utf-8"))
    value = meta.get("public_b64")
    return str(value) if value is not None else None


def first_difference(left: str, right: str) -> int:
    for i, (a, b) in enumerate(zip(left, right, strict=False)):
        if a != b:
            return i
    return min(len(left), len(right))


def compare_public(derived: str, expected: str, source: str) -> str | None:
    """None when they agree, otherwise the abort line, naming the byte."""
    if derived == expected:
        return None
    at = first_difference(derived, expected)
    return (
        f"ABORT: the converted public key differs from {source} at character {at}\n"
        f"  converted: {derived}\n"
        f"  {source}: {expected}\n"
        "  every existing push subscription would go silent, with no error anywhere in the UI"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m prep.migrate.vapid",
        description="Convert a Python VAPID keypair into the worker's two base64url vars.",
    )
    parser.add_argument("--pem", required=True, type=Path, help="vapid-private.pem")
    parser.add_argument(
        "--keys", type=Path, help="vapid-keys.json, whose public_b64 the conversion must reproduce"
    )
    parser.add_argument(
        "--expect", help="the live app's /notify/vapid-public-key, compared byte for byte"
    )
    args = parser.parse_args(argv)

    try:
        pair = convert_pem(Path(args.pem).read_bytes())
    except (OSError, ValueError, VapidConversionError) as e:
        print(f"ABORT: {args.pem}: {e}", file=sys.stderr)
        return 1

    failures = []
    if args.keys is not None:
        recorded = recorded_public_key(Path(args.keys))
        if recorded is None:
            failures.append(
                f"ABORT: {args.keys} has no public_b64, so the PEM cannot be checked against the deploy"
            )
        else:
            failures.append(compare_public(pair.public_key, recorded, "vapid-keys.json"))
    if args.expect is not None:
        failures.append(compare_public(pair.public_key, args.expect.strip(), "the live key"))

    for failure in [f for f in failures if f]:
        print(failure, file=sys.stderr)
    if any(failures):
        return 1

    print(pair.env_lines())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
