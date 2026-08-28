// Web push, pinned two ways: the encrypted body against a vector another
// implementation produced (fixtures/webpush-vector.json, written by the
// library pywebpush encrypts with), and the VAPID header against a
// verifier holding only the public key.
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { b64uDecode, b64uEncode } from '../../domain/base64.js';
import { encryptPayload, JWT_TTL_SECONDS, vapidHeader, WebCryptoWebPush } from '../../runtime/adapters/webpush.js';

const TEXT = new TextEncoder();
const bytes = (b64u: string): Uint8Array => b64uDecode(b64u)!;

interface Vector {
  source: string;
  plaintext: string;
  ua_public: string;
  ua_private: string;
  auth_secret: string;
  as_public: string;
  as_private: string;
  salt: string;
  body: string;
}

const VECTOR: Vector = JSON.parse(readFileSync(new URL('./fixtures/webpush-vector.json', import.meta.url).pathname, 'utf8'));

type Usage = 'sign' | 'deriveBits';

async function ecKeyPair(publicB64u: string, privateB64u: string, usages: Usage[]): Promise<CryptoKeyPair> {
  const pub = bytes(publicB64u);
  const jwk: JsonWebKey = { kty: 'EC', crv: 'P-256', x: b64uEncode(pub.slice(1, 33)), y: b64uEncode(pub.slice(33, 65)), ext: true };
  const algorithm = { name: usages.includes('sign') ? 'ECDSA' : 'ECDH', namedCurve: 'P-256' };
  const publicKey = await crypto.subtle.importKey('jwk', jwk, algorithm, true, usages.includes('sign') ? ['verify'] : []);
  const privateKey = await crypto.subtle.importKey('jwk', { ...jwk, d: b64uEncode(bytes(privateB64u)) }, algorithm, true, usages);
  return { publicKey, privateKey };
}

describe('aes128gcm message encryption', () => {
  it('reproduces another implementation\'s record byte for byte', async () => {
    const ephemeral = await ecKeyPair(VECTOR.as_public, VECTOR.as_private, ['deriveBits']);
    const body = await encryptPayload(
      { p256dh: VECTOR.ua_public, auth: VECTOR.auth_secret },
      TEXT.encode(VECTOR.plaintext),
      bytes(VECTOR.salt),
      ephemeral,
    );
    expect(b64uEncode(body)).toBe(VECTOR.body);
  });

  it('lays the record header out as aes128gcm requires', async () => {
    const ephemeral = await ecKeyPair(VECTOR.as_public, VECTOR.as_private, ['deriveBits']);
    const body = await encryptPayload({ p256dh: VECTOR.ua_public, auth: VECTOR.auth_secret }, TEXT.encode('x'), bytes(VECTOR.salt), ephemeral);
    expect(b64uEncode(body.slice(0, 16))).toBe(VECTOR.salt);
    expect(new DataView(body.buffer, body.byteOffset + 16, 4).getUint32(0, false)).toBe(4096);
    expect(body[20]).toBe(65);
    expect(b64uEncode(body.slice(21, 86))).toBe(VECTOR.as_public);
  });

  it('round-trips through a subscription keypair', async () => {
    const ua = (await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])) as CryptoKeyPair;
    const uaPublic = new Uint8Array((await crypto.subtle.exportKey('raw', ua.publicKey)) as ArrayBuffer);
    const auth = crypto.getRandomValues(new Uint8Array(16));
    const ephemeral = (await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])) as CryptoKeyPair;
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const payload = JSON.stringify({ title: 'Prep', body: 'one card is due', url: '/study/world-capitals' });

    const body = await encryptPayload({ p256dh: b64uEncode(uaPublic), auth: b64uEncode(auth) }, TEXT.encode(payload), salt, ephemeral);
    expect(await decrypt(body, ua.privateKey, uaPublic, auth)).toBe(payload);
  });
});

/** The receiving half of RFC 8291: the record header is read back off the
 * wire rather than assumed from the encryptor. */
async function decrypt(body: Uint8Array, uaPrivate: CryptoKey, uaPublic: Uint8Array, auth: Uint8Array): Promise<string> {
  const salt = body.slice(0, 16);
  const idlen = body[20]!;
  const asPublic = body.slice(21, 21 + idlen);
  const ciphertext = body.slice(21 + idlen);
  const asKey = await crypto.subtle.importKey('raw', asPublic as BufferSource, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: 'ECDH', public: asKey } as never, uaPrivate, 256));

  const info = new Uint8Array(TEXT.encode('WebPush: info').length + 1 + uaPublic.length + asPublic.length);
  info.set(TEXT.encode('WebPush: info'), 0);
  info.set(uaPublic, TEXT.encode('WebPush: info').length + 1);
  info.set(asPublic, TEXT.encode('WebPush: info').length + 1 + uaPublic.length);

  const expand = async (salt2: Uint8Array, ikm: Uint8Array, label: Uint8Array, length: number) => {
    const key = await crypto.subtle.importKey('raw', ikm as BufferSource, 'HKDF', false, ['deriveBits']);
    return new Uint8Array(
      await crypto.subtle.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt: salt2 as BufferSource, info: label as BufferSource }, key, length * 8),
    );
  };
  const ikm = await expand(auth, shared, info, 32);
  const cek = await expand(salt, ikm, TEXT.encode('Content-Encoding: aes128gcm\0'), 16);
  const nonce = await expand(salt, ikm, TEXT.encode('Content-Encoding: nonce\0'), 12);
  const aes = await crypto.subtle.importKey('raw', cek as BufferSource, 'AES-GCM', false, ['decrypt']);
  const plain = new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: nonce as BufferSource }, aes, ciphertext as BufferSource));
  expect(plain[plain.length - 1]).toBe(2);
  return new TextDecoder().decode(plain.slice(0, -1));
}

describe('the VAPID authorization', () => {
  const KEYS = {
    publicKey: VECTOR.as_public,
    privateKey: VECTOR.as_private,
    subject: 'mailto:ops@example.test',
  };

  it('verifies under the public key and carries the endpoint origin', async () => {
    const now = Date.UTC(2026, 2, 14, 15, 0, 0) / 1000;
    const header = await vapidHeader(KEYS, 'https://push.example.test', now);
    const [, t, k] = /^vapid t=([^,]+), k=(.+)$/.exec(header)!;
    expect(k).toBe(VECTOR.as_public);
    const [h, p, sig] = t!.split('.');
    const { publicKey } = await ecKeyPair(VECTOR.as_public, VECTOR.as_private, ['sign']);
    const ok = await crypto.subtle.verify(
      { name: 'ECDSA', hash: 'SHA-256' },
      publicKey,
      bytes(sig!) as BufferSource,
      TEXT.encode(`${h}.${p}`) as BufferSource,
    );
    expect(ok).toBe(true);
    expect(JSON.parse(new TextDecoder().decode(bytes(h!)))).toEqual({ typ: 'JWT', alg: 'ES256' });
    expect(JSON.parse(new TextDecoder().decode(bytes(p!)))).toEqual({
      aud: 'https://push.example.test',
      exp: now + JWT_TTL_SECONDS,
      sub: 'mailto:ops@example.test',
    });
  });
});

describe('the sender', () => {
  const subscription = { endpoint: 'https://push.example.test/sub', p256dh: VECTOR.ua_public, auth: VECTOR.auth_secret };
  const keys = { publicKey: VECTOR.as_public, privateKey: VECTOR.as_private, subject: 'mailto:ops@example.test' };
  const at = () => new Date('2026-03-14T15:00:00Z');

  async function sendWith(status: number): Promise<{ outcome: string; request: Request | null }> {
    let seen: Request | null = null;
    const original = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      seen = new Request(input as RequestInfo, init);
      return new Response(null, { status });
    }) as typeof fetch;
    try {
      return { outcome: await new WebCryptoWebPush(keys, at).send(subscription, '{"title":"x"}'), request: seen };
    } finally {
      globalThis.fetch = original;
    }
  }

  it('sends the encrypted record under the push headers', async () => {
    const { outcome, request } = await sendWith(201);
    expect(outcome).toBe('ok');
    expect(request!.headers.get('content-encoding')).toBe('aes128gcm');
    expect(request!.headers.get('ttl')).toBe('60');
    expect(request!.headers.get('authorization')).toMatch(/^vapid t=[\w-]+\.[\w-]+\.[\w-]+, k=/);
  });

  it.each([404, 410])('prunes on %i', async (status) => {
    expect((await sendWith(status)).outcome).toBe('gone');
  });

  it('counts any other refusal as a failure', async () => {
    expect((await sendWith(500)).outcome).toBe('fail');
  });
});
