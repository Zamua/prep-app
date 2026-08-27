// Web Push over WebCrypto: the VAPID authorization (RFC 8292, ES256) and
// the message encryption (RFC 8291 `aes128gcm`, one record). A 404 or 410
// means the subscription is gone and the caller prunes it; any other
// failure counts as `fail` and never raises.
import { hkdfSha256 } from './hkdf.js';
import { b64uDecode, b64uEncode } from '../../domain/base64.js';
import type { PushSubscription } from '../../app/entities.js';
import type { PushOutcome, WebPush } from '../../app/ports.js';

const TEXT = new TextEncoder();

/** The VAPID JWT's lifetime; push services refuse anything past 24 h. */
export const JWT_TTL_SECONDS = 12 * 60 * 60;
export const PUSH_TTL_SECONDS = 60;
const RECORD_SIZE = 4096;
const KEY_LENGTH = 16;
const NONCE_LENGTH = 12;
const SALT_LENGTH = 16;

export class VapidKeyError extends Error {}

export interface VapidKeys {
  /** The uncompressed P-256 point, base64url: what the browser subscribes against. */
  publicKey: string;
  /** The 32-byte scalar, base64url. */
  privateKey: string;
  /** The `sub` claim: a contact for the push service's operators. */
  subject: string;
}

function decodeOrThrow(value: string, what: string, length?: number): Uint8Array {
  const raw = b64uDecode(value.trim());
  if (raw === null || (length !== undefined && raw.length !== length)) throw new VapidKeyError(`${what} is not ${length ?? 'valid'} base64url bytes`);
  return raw;
}

/** A JWK for the VAPID keypair; ECDSA over WebCrypto needs both halves. */
function vapidJwk(keys: VapidKeys): JsonWebKey {
  const pub = decodeOrThrow(keys.publicKey, 'PREP_VAPID_PUBLIC_KEY', 65);
  if (pub[0] !== 0x04) throw new VapidKeyError('PREP_VAPID_PUBLIC_KEY is not an uncompressed P-256 point');
  const d = decodeOrThrow(keys.privateKey, 'PREP_VAPID_PRIVATE_KEY', 32);
  return {
    kty: 'EC',
    crv: 'P-256',
    x: b64uEncode(pub.slice(1, 33)),
    y: b64uEncode(pub.slice(33, 65)),
    d: b64uEncode(d),
    ext: true,
  };
}

export async function vapidHeader(keys: VapidKeys, audience: string, nowSeconds: number): Promise<string> {
  const key = await crypto.subtle
    .importKey('jwk', vapidJwk(keys), { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign'])
    .catch((e) => {
      throw new Error(`importKey(jwk ECDSA vapid): ${e instanceof Error ? e.message : e}`);
    });
  const header = b64uEncode(TEXT.encode(JSON.stringify({ typ: 'JWT', alg: 'ES256' })));
  const payload = b64uEncode(TEXT.encode(JSON.stringify({ aud: audience, exp: Math.floor(nowSeconds) + JWT_TTL_SECONDS, sub: keys.subject })));
  const signing = `${header}.${payload}`;
  const signature = new Uint8Array(await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, key, TEXT.encode(signing)));
  return `vapid t=${signing}.${b64uEncode(signature)}, k=${keys.publicKey.trim()}`;
}

const concat = (...parts: Uint8Array[]): Uint8Array => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
};

/** The 65-byte uncompressed P-256 point `04 || x || y`.
 *
 * celld's `exportKey('raw')` answers SPKI DER for an EC key, not the point
 * RFC 8291 puts in the `keyid`; a 91-byte DER blob there encrypts fine and
 * decrypts nowhere, so the browser drops the notification in silence. The
 * JWK coordinates are the same key in a shape both runtimes agree on.
 */
async function uncompressedPoint(key: CryptoKey): Promise<Uint8Array> {
  const jwk = (await crypto.subtle.exportKey('jwk', key)) as JsonWebKey;
  const x = b64uDecode(jwk.x ?? '');
  const y = b64uDecode(jwk.y ?? '');
  if (x === null || y === null || x.length !== 32 || y.length !== 32) {
    throw new VapidKeyError('the ephemeral key did not export as a P-256 point');
  }
  return concat(new Uint8Array([0x04]), x, y);
}


const hkdf = (salt: Uint8Array, ikm: Uint8Array, info: Uint8Array, length: number): Promise<Uint8Array> =>
  hkdfSha256(ikm, info, length, salt);

/**
 * One `aes128gcm` record: `salt | rs | idlen | as_public | ciphertext`,
 * with the plaintext padded by the single last-record delimiter 0x02.
 */
export async function encryptPayload(
  subscription: { p256dh: string; auth: string },
  plaintext: Uint8Array,
  salt: Uint8Array,
  ephemeral: CryptoKeyPair,
): Promise<Uint8Array> {
  const uaPublic = b64uDecode(subscription.p256dh.trim());
  const authSecret = b64uDecode(subscription.auth.trim());
  if (uaPublic === null || uaPublic.length !== 65 || authSecret === null) throw new VapidKeyError('subscription keys are not valid base64url');

  // celld's WebCrypto has no `raw` import for ECDH, so the uncompressed
  // P-256 point is split into its coordinates and imported as a JWK.
  if (uaPublic[0] !== 0x04) throw new VapidKeyError('subscription p256dh is not an uncompressed P-256 point');
  const uaKey = await crypto.subtle.importKey(
    'jwk',
    { kty: 'EC', crv: 'P-256', x: b64uEncode(uaPublic.slice(1, 33)), y: b64uEncode(uaPublic.slice(33, 65)), ext: true },
    { name: 'ECDH', namedCurve: 'P-256' },
    false,
    [],
  );
  // workers-types spells ECDH's peer key `$public`; the runtime reads either.
  const ecdh = { name: 'ECDH', public: uaKey } as unknown as SubtleCryptoDeriveKeyAlgorithm;
  const shared = new Uint8Array(await crypto.subtle.deriveBits(ecdh, ephemeral.privateKey, 256));
  const asPublic = await uncompressedPoint(ephemeral.publicKey);

  // RFC 8291 section 3.4: the auth secret salts the ECDH secret, and the
  // two public keys bind the derivation to this exact pair.
  const keyInfo = concat(TEXT.encode('WebPush: info'), new Uint8Array([0]), uaPublic, asPublic);
  const ikm = await hkdf(authSecret, shared, keyInfo, 32);
  const cek = await hkdf(salt, ikm, TEXT.encode('Content-Encoding: aes128gcm\0'), KEY_LENGTH);
  const nonce = await hkdf(salt, ikm, TEXT.encode('Content-Encoding: nonce\0'), NONCE_LENGTH);

  const aes = await crypto.subtle.importKey('raw', cek as BufferSource, 'AES-GCM', false, ['encrypt']);
  const padded = concat(plaintext, new Uint8Array([2]));
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce as BufferSource }, aes, padded as BufferSource));

  const rs = new Uint8Array(4);
  new DataView(rs.buffer).setUint32(0, RECORD_SIZE, false);
  return concat(salt, rs, new Uint8Array([asPublic.length]), asPublic, ciphertext);
}

export class WebCryptoWebPush implements WebPush {
  constructor(
    private readonly keys: VapidKeys,
    private readonly now: () => Date,
  ) {}

  async send(subscription: PushSubscription, payload: string): Promise<PushOutcome> {
    const host = (() => {
      try {
        return new URL(subscription.endpoint).host;
      } catch {
        return '<unparseable>';
      }
    })();
    let body: Uint8Array;
    let authorization: string;
    // Names the failing primitive: celld's WebCrypto does not accept every
    // import shape Node does, and a bare message cannot say which one.
    let step = 'start';
    try {
      const salt = crypto.getRandomValues(new Uint8Array(SALT_LENGTH));
      step = 'generateKey(ECDH)';
      const ephemeral = (await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])) as CryptoKeyPair;
      step = 'encryptPayload';
      body = await encryptPayload(subscription, TEXT.encode(payload), salt, ephemeral);
      step = 'vapidHeader';
      authorization = await vapidHeader(this.keys, new URL(subscription.endpoint).origin, this.now().getTime() / 1000);
    } catch (e) {
      console.error(`web push: ${step} failed for ${host}: ${e instanceof Error ? `${e.name}: ${e.message}` : e}`);
      return 'fail';
    }
    try {
      const res = await fetch(subscription.endpoint, {
        method: 'POST',
        headers: {
          authorization,
          'content-encoding': 'aes128gcm',
          'content-type': 'application/octet-stream',
          ttl: String(PUSH_TTL_SECONDS),
        },
        body: body as BodyInit,
      });
      if (res.status === 404 || res.status === 410) return 'gone';
      if (!res.ok) {
        // The push service's own words. Without them a rejected VAPID token
        // and a network blip are the same bare 'fail'.
        console.error(`web push: ${host} answered ${res.status}: ${(await res.text().catch(() => '')).slice(0, 300)}`);
        return 'fail';
      }
      return 'ok';
    } catch (e) {
      console.error(`web push: ${host} unreachable: ${e instanceof Error ? e.message : e}`);
      return 'fail';
    }
  }
}

/** No keys configured: every send fails and nothing is pruned. */
export class NoWebPush implements WebPush {
  async send(): Promise<PushOutcome> {
    return 'fail';
  }
}
