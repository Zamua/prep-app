import { describe, expect, it } from 'vitest';
import { DecryptionError } from '../app/ports';
import { b64Decode, b64Encode, hexToBytes } from '../domain/base64';
import { AesGcmCipher, KEY_LEN, loadMasterKey, MasterKeyError, NONCE_LEN } from '../runtime/adapters/byokCrypto';
import { pythonJson } from './pyoracle';

const MASTER_HEX = '11'.repeat(32);
const MASTER = hexToBytes(MASTER_HEX)!;
const SECRET = 'sk-ant-api03-parity-fixture-key-0000';

/** A counter, not randomness: the nonce only has to be fresh per message. */
function counterRandom(seed = 0) {
  let n = seed;
  return {
    bytes(size: number): Uint8Array {
      const out = new Uint8Array(size);
      out[out.length - 1] = n++ & 0xff;
      return out;
    },
    choice<T>(seq: readonly T[]): T {
      return seq[0]!;
    },
  };
}

const cipher = () => new AesGcmCipher(MASTER, counterRandom());

describe('the master key', () => {
  it('accepts 32 hex bytes and names what is wrong otherwise', () => {
    expect(loadMasterKey({ PREP_KEY_ENCRYPTION_SECRET: MASTER_HEX })).toEqual(MASTER);
    expect(loadMasterKey({ PREP_KEY_ENCRYPTION_SECRET: `  ${MASTER_HEX}\n` })).toEqual(MASTER);
    expect(() => loadMasterKey({})).toThrow(MasterKeyError);
    expect(() => loadMasterKey({ PREP_KEY_ENCRYPTION_SECRET: 'not-hex-at-all' })).toThrow(/hex-encoded/);
    expect(() => loadMasterKey({ PREP_KEY_ENCRYPTION_SECRET: '00'.repeat(16) })).toThrow(/32 bytes/);
    expect(() => new AesGcmCipher(new Uint8Array(16), counterRandom())).toThrow(MasterKeyError);
  });
});

describe('AES-256-GCM round trips', () => {
  it('nonce, ciphertext and tag concatenated under base64', async () => {
    const blob = await cipher().encrypt(SECRET);
    const raw = b64Decode(blob)!;
    expect(raw).not.toBeNull();
    // 12-byte nonce, the plaintext's own length, and a 16-byte tag.
    expect(raw.length).toBe(NONCE_LEN + SECRET.length + 16);
    expect(await cipher().decrypt(blob)).toBe(SECRET);
  });

  it('refuses an empty plaintext and every corrupt blob', async () => {
    await expect(cipher().encrypt('')).rejects.toThrow(RangeError);
    await expect(cipher().decrypt('')).rejects.toThrow(DecryptionError);
    await expect(cipher().decrypt('not base64!')).rejects.toThrow(/not valid base64/);
    await expect(cipher().decrypt(b64Encode(new Uint8Array(20)))).rejects.toThrow(/too short/);
    const blob = await cipher().encrypt(SECRET);
    const raw = b64Decode(blob)!;
    raw[raw.length - 1] = (raw[raw.length - 1] ?? 0) ^ 1;
    await expect(cipher().decrypt(b64Encode(raw))).rejects.toThrow(/tag mismatch/);
    const other = new AesGcmCipher(hexToBytes('22'.repeat(KEY_LEN))!, counterRandom());
    await expect(other.decrypt(blob)).rejects.toThrow(DecryptionError);
  });
});

describe('byte compatibility with the Python app', () => {
  // The rows in `byok_credentials` were written by prep/byok/crypto.py and
  // must decrypt here with the same master key, or phase 6 loses every key.
  const fromPython = pythonJson<{ blob: string; roundtrip: string }>(
    `import json
from prep.byok.crypto import encrypt, decrypt
key = bytes.fromhex("${MASTER_HEX}")
blob = encrypt(${JSON.stringify(SECRET)}, key)
print(json.dumps({"blob": blob, "roundtrip": decrypt(blob, key)}))`,
  );

  it('decrypts a ciphertext the Python app produced', async () => {
    expect(fromPython.roundtrip).toBe(SECRET);
    expect(await cipher().decrypt(fromPython.blob)).toBe(SECRET);
  });

  it('produces a ciphertext the Python app decrypts', async () => {
    const blob = await cipher().encrypt(SECRET);
    const back = pythonJson<{ plain: string }>(
      `import json
from prep.byok.crypto import decrypt
print(json.dumps({"plain": decrypt(${JSON.stringify(blob)}, bytes.fromhex("${MASTER_HEX}"))}))`,
    );
    expect(back.plain).toBe(SECRET);
  });

  it('carries non-ASCII plaintext through unchanged', async () => {
    const text = 'sk-or-v1-café-é中文';
    const blob = await cipher().encrypt(text);
    const back = pythonJson<{ plain: string }>(
      `import json
from prep.byok.crypto import decrypt
print(json.dumps({"plain": decrypt(${JSON.stringify(blob)}, bytes.fromhex("${MASTER_HEX}"))}))`,
    );
    expect(back.plain).toBe(text);
    expect(await cipher().decrypt(blob)).toBe(text);
  });
});
