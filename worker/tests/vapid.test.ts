// The VAPID keypair as the push path uses it: the header signs under the
// application server key a subscription was created with, and the payload
// encryption does not involve that key at all.
//
// The keypair is generated per run rather than committed. It is also the
// point of the test: a fresh keypair is exactly what must NOT happen to a
// deploy that already handed browsers a public key, and the third case here
// is what that failure looks like.
import { beforeAll, describe, expect, it } from 'vitest';
import { b64uDecode, b64uEncode } from '../domain/base64.js';
import { encryptPayload, vapidHeader, VapidKeyError, type VapidKeys } from '../runtime/adapters/webpush.js';

const SUBJECT = 'mailto:noreply@example.test';
const AUDIENCE = 'https://push.example.test';
const NOW_SECONDS = 1787000000;

/** The shape the app stores: the uncompressed P-256 point and the 32-byte
 * scalar, both base64url. */
async function generate(): Promise<{ publicKey: string; privateKey: string }> {
  const pair = (await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign', 'verify'])) as CryptoKeyPair;
  const raw = new Uint8Array((await crypto.subtle.exportKey('raw', pair.publicKey)) as ArrayBuffer);
  const jwk = (await crypto.subtle.exportKey('jwk', pair.privateKey)) as JsonWebKey;
  return { publicKey: b64uEncode(raw), privateKey: jwk.d! };
}

let keys: VapidKeys;
let other: VapidKeys;
beforeAll(async () => {
  keys = { ...(await generate()), subject: SUBJECT };
  other = { ...(await generate()), subject: SUBJECT };
});

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

describe('the VAPID header', () => {
  it('carries the uncompressed point a subscription names', () => {
    const raw = b64uDecode(keys.publicKey)!;
    expect(raw).toHaveLength(65);
    expect(raw[0]).toBe(0x04);
    expect(b64uDecode(keys.privateKey)).toHaveLength(32);
  });

  it('signs a JWT that verifies under that same public key', async () => {
    const { signing, signature, k, claims } = parseHeader(await vapidHeader(keys, AUDIENCE, NOW_SECONDS));
    expect(k).toBe(keys.publicKey);
    expect(claims['aud']).toBe(AUDIENCE);
    expect(claims['sub']).toBe(SUBJECT);
    expect(claims['exp']).toBeGreaterThan(NOW_SECONDS);
    expect(await verifyUnder(keys.publicKey, signing, signature)).toBe(true);
  });

  it('is what stops every existing subscription going silent', async () => {
    // A different keypair signs a JWT the push service answers 403 to,
    // because it does not match the `applicationServerKey` the subscription
    // carries. Nothing in the UI says so, which is why the keypair is
    // persisted rather than minted per boot.
    const { signing, signature } = parseHeader(await vapidHeader(other, AUDIENCE, NOW_SECONDS));
    expect(await verifyUnder(keys.publicKey, signing, signature)).toBe(false);
    expect(await verifyUnder(other.publicKey, signing, signature)).toBe(true);
  });

  it('refuses a public key that is not an uncompressed P-256 point', async () => {
    const compressed = b64uEncode(new Uint8Array([0x02, ...b64uDecode(keys.publicKey)!.slice(1, 33)]));
    await expect(vapidHeader({ ...keys, publicKey: compressed }, AUDIENCE, NOW_SECONDS)).rejects.toBeInstanceOf(VapidKeyError);
    await expect(vapidHeader({ ...keys, privateKey: 'short' }, AUDIENCE, NOW_SECONDS)).rejects.toBeInstanceOf(VapidKeyError);
  });
});

describe('the payload encryption', () => {
  it('encrypts under the subscription own keys, never the application server key', async () => {
    // RFC 8291 derives the content key from the subscription's p256dh and
    // auth plus a fresh ephemeral pair. The VAPID keypair is not an input,
    // so a `push_subscriptions` row is unaffected by a key rotation.
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
