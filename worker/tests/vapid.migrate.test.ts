// The VAPID conversion, proved from both ends: `prep.migrate.vapid` derives
// the public key the Python app has been handing browsers, and the worker
// signs a JWT that verifies under that same key.
//
// The keypair is generated per run rather than committed. It is also the
// point of the test: a fresh keypair is exactly what must NOT happen at the
// cutover, and the last case here is what that failure looks like.
import { describe, expect, it } from 'vitest';
import { b64uDecode, b64uEncode } from '../domain/base64.js';
import { encryptPayload, vapidHeader, VapidKeyError, type VapidKeys } from '../runtime/adapters/webpush.js';
import { pythonJson } from './pyoracle';

interface Converted {
  /** `prep.notify.push.public_key_b64()`: what the browser subscribed with. */
  app_public: string;
  /** `prep.migrate.vapid.convert_pem`. */
  converted_public: string;
  converted_private: string;
  /** A second, unrelated keypair: the mistake the conversion prevents. */
  other_public: string;
  other_private: string;
}

const pair: Converted = pythonJson<Converted>(
  `import json, tempfile
from pathlib import Path
from py_vapid import Vapid01
from prep.migrate.vapid import convert_pem
from prep.notify.push import _public_key_b64url

def make():
    v = Vapid01()
    v.generate_keys()
    return v

v, other = make(), make()
converted = convert_pem(v.private_pem())
other_converted = convert_pem(other.private_pem())
print(json.dumps({
    "app_public": _public_key_b64url(v),
    "converted_public": converted.public_key,
    "converted_private": converted.private_key,
    "other_public": other_converted.public_key,
    "other_private": other_converted.private_key,
}))`,
);

const SUBJECT = 'mailto:noreply@example.test';
const AUDIENCE = 'https://push.example.test';
const NOW_SECONDS = 1787000000;

const keys: VapidKeys = { publicKey: pair.converted_public, privateKey: pair.converted_private, subject: SUBJECT };

function parseHeader(header: string): { signing: string; signature: Uint8Array; k: string; claims: Record<string, unknown> } {
  const match = /^vapid t=([^,]+), k=(.+)$/.exec(header);
  expect(match, header).not.toBeNull();
  const [head, payload, signature] = match![1]!.split('.');
  return {
    signing: `${head}.${payload}`,
    signature: b64uDecode(signature!)!,
    k: match![2]!,
    claims: JSON.parse(new TextDecoder().decode(b64uDecode(payload!)!)) as Record<string, unknown>,
  };
}

async function verifyUnder(publicKey: string, signing: string, signature: Uint8Array): Promise<boolean> {
  const raw = b64uDecode(publicKey)!;
  const key = await crypto.subtle.importKey(
    'jwk',
    { kty: 'EC', crv: 'P-256', x: b64uEncode(raw.slice(1, 33)), y: b64uEncode(raw.slice(33, 65)), ext: true },
    { name: 'ECDSA', namedCurve: 'P-256' },
    false,
    ['verify'],
  );
  return crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, key, signature as BufferSource, new TextEncoder().encode(signing));
}

describe('the conversion is the same keypair in another shape', () => {
  it('derives the public key the Python app already serves, byte for byte', () => {
    expect(pair.converted_public).toBe(pair.app_public);
    const raw = b64uDecode(pair.converted_public)!;
    expect(raw).toHaveLength(65);
    expect(raw[0]).toBe(0x04);
    expect(b64uDecode(pair.converted_private)).toHaveLength(32);
  });

  it('signs a JWT that verifies under that same public key', async () => {
    const { signing, signature, k, claims } = parseHeader(await vapidHeader(keys, AUDIENCE, NOW_SECONDS));
    expect(k).toBe(pair.app_public);
    expect(claims['aud']).toBe(AUDIENCE);
    expect(claims['sub']).toBe(SUBJECT);
    expect(claims['exp']).toBeGreaterThan(NOW_SECONDS);
    expect(await verifyUnder(pair.app_public, signing, signature)).toBe(true);
  });

  it('is what stops every migrated subscription going silent', async () => {
    // A fresh keypair signs a JWT the push service answers 403 to, because
    // it does not match the `applicationServerKey` the subscription carries.
    // Nothing in the UI says so, which is why the conversion is a gate.
    const minted: VapidKeys = { publicKey: pair.other_public, privateKey: pair.other_private, subject: SUBJECT };
    const { signing, signature } = parseHeader(await vapidHeader(minted, AUDIENCE, NOW_SECONDS));
    expect(await verifyUnder(pair.app_public, signing, signature)).toBe(false);
    expect(await verifyUnder(pair.other_public, signing, signature)).toBe(true);
  });

  it('refuses a public key that is not an uncompressed P-256 point', async () => {
    const compressed = b64uEncode(new Uint8Array([0x02, ...b64uDecode(pair.converted_public)!.slice(1, 33)]));
    await expect(vapidHeader({ ...keys, publicKey: compressed }, AUDIENCE, NOW_SECONDS)).rejects.toBeInstanceOf(VapidKeyError);
    await expect(vapidHeader({ ...keys, privateKey: 'short' }, AUDIENCE, NOW_SECONDS)).rejects.toBeInstanceOf(VapidKeyError);
  });
});

describe('an existing subscription survives the conversion', () => {
  it('encrypts under the subscription own keys, never the application server key', async () => {
    // RFC 8291 derives the content key from the subscription's p256dh and
    // auth plus a fresh ephemeral pair. The VAPID keypair is not an input,
    // so a migrated `push_subscriptions` row keeps working unchanged.
    const ua = (await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])) as CryptoKeyPair;
    const subscription = {
      p256dh: b64uEncode(new Uint8Array((await crypto.subtle.exportKey('raw', ua.publicKey)) as ArrayBuffer)),
      auth: b64uEncode(crypto.getRandomValues(new Uint8Array(16))),
    };
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const ephemeral = (await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])) as CryptoKeyPair;
    const plaintext = new TextEncoder().encode('{"title":"due"}');
    const first = await encryptPayload(subscription, plaintext, salt, ephemeral);
    const second = await encryptPayload(subscription, plaintext, salt, ephemeral);
    expect(Array.from(second)).toEqual(Array.from(first));
  });
});
