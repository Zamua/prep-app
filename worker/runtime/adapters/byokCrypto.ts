// AES-256-GCM envelope encryption for BYOK secrets: 12-byte nonce, no AAD,
// `base64(nonce || ct || tag)`, master key 32 hex bytes. The layout is fixed
// by the rows already stored; every saved key is unreadable if it moves.
import type { Cipher, Random } from '../../app/ports.js';
import { DecryptionError } from '../../app/ports.js';
import { b64Decode, b64Encode, hexToBytes } from '../../domain/base64.js';

export const NONCE_LEN = 12;
export const KEY_LEN = 32;
export const TAG_LEN = 16;

export class MasterKeyError extends Error {}

export interface MasterKeyEnv {
  PREP_KEY_ENCRYPTION_SECRET?: string;
}

/** The master key, or the reason it cannot be used. Boot fails closed. */
export function loadMasterKey(env: MasterKeyEnv, envVar = 'PREP_KEY_ENCRYPTION_SECRET'): Uint8Array {
  const raw = (env.PREP_KEY_ENCRYPTION_SECRET ?? '').trim();
  if (!raw) throw new MasterKeyError(`${envVar} is not set. Generate one with \`openssl rand -hex 32\`.`);
  const key = hexToBytes(raw);
  if (!key) throw new MasterKeyError(`${envVar} must be hex-encoded (e.g. \`openssl rand -hex 32\`).`);
  if (key.length !== KEY_LEN) throw new MasterKeyError(`${envVar} must decode to ${KEY_LEN} bytes (256 bits). Got ${key.length} bytes.`);
  return key;
}

export class AesGcmCipher implements Cipher {
  private key: Promise<CryptoKey> | null = null;

  constructor(
    private readonly master: Uint8Array,
    private readonly random: Random,
  ) {
    if (master.length !== KEY_LEN) throw new MasterKeyError(`master key must be ${KEY_LEN} bytes, got ${master.length}`);
  }

  private material(): Promise<CryptoKey> {
    this.key ??= crypto.subtle.importKey('raw', this.master as unknown as BufferSource, 'AES-GCM', false, ['encrypt', 'decrypt']);
    return this.key;
  }

  async encrypt(plaintext: string): Promise<string> {
    if (!plaintext) throw new RangeError('cannot encrypt empty plaintext');
    const nonce = this.random.bytes(NONCE_LEN);
    const sealed = new Uint8Array(
      await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv: nonce as unknown as BufferSource, tagLength: TAG_LEN * 8 },
        await this.material(),
        new TextEncoder().encode(plaintext) as unknown as BufferSource,
      ),
    );
    const out = new Uint8Array(nonce.length + sealed.length);
    out.set(nonce, 0);
    out.set(sealed, nonce.length);
    return b64Encode(out);
  }

  async decrypt(ciphertext: string): Promise<string> {
    if (!ciphertext) throw new DecryptionError('empty ciphertext blob');
    const raw = b64Decode(ciphertext);
    if (!raw) throw new DecryptionError('ciphertext is not valid base64');
    if (raw.length < NONCE_LEN + TAG_LEN) throw new DecryptionError('ciphertext blob too short');
    try {
      const plain = await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: raw.subarray(0, NONCE_LEN) as unknown as BufferSource, tagLength: TAG_LEN * 8 },
        await this.material(),
        raw.subarray(NONCE_LEN) as unknown as BufferSource,
      );
      return new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(plain);
    } catch {
      throw new DecryptionError('AES-GCM tag mismatch: master key may have rotated or ciphertext is corrupt.');
    }
  }
}
