// HKDF-SHA256 (RFC 5869) built from HMAC, matching `cryptography`'s HKDF with
// `salt=None`. Written out rather than imported as an `HKDF` WebCrypto key
// because the celld runtime supports no such key import, only HMAC.

async function hmacSha256(key: Uint8Array, data: Uint8Array): Promise<Uint8Array> {
  const k = await crypto.subtle.importKey('raw', key as unknown as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', k, data as unknown as BufferSource));
}

const HASH_LENGTH = 32;

export async function hkdfSha256(ikm: Uint8Array, info: Uint8Array, length: number, salt: Uint8Array = new Uint8Array(0)): Promise<Uint8Array> {
  // RFC 5869 defaults an absent salt to HashLen zero bytes; spelling it out
  // also keeps the extract step off a zero-length HMAC key, which is refused.
  const prk = await hmacSha256(salt.length ? salt : new Uint8Array(HASH_LENGTH), ikm);
  const out = new Uint8Array(length);
  let block: Uint8Array = new Uint8Array(0);
  for (let counter = 1, written = 0; written < length; counter++) {
    const input = new Uint8Array(block.length + info.length + 1);
    input.set(block, 0);
    input.set(info, block.length);
    input[input.length - 1] = counter;
    block = await hmacSha256(prk, input);
    out.set(block.subarray(0, Math.min(block.length, length - written)), written);
    written += block.length;
  }
  return out;
}
