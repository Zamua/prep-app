// HKDF-SHA256 over WebCrypto, matching `cryptography`'s HKDF with
// `salt=None`: RFC 5869 defaults an absent salt to HashLen zero bytes, and
// HMAC pads either form to the same block, so an empty salt is the same key.

export async function hkdfSha256(ikm: Uint8Array, info: Uint8Array, length: number, salt = new Uint8Array(0)): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey('raw', ikm as unknown as BufferSource, 'HKDF', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: salt as unknown as BufferSource, info: info as unknown as BufferSource },
    key,
    length * 8,
  );
  return new Uint8Array(bits);
}
