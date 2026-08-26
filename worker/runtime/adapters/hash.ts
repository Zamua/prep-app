import type { Hasher } from '../../app/ports.js';

const hex = (bytes: Uint8Array): string => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');

export class WebCryptoHasher implements Hasher {
  async sha256Hex(text: string): Promise<string> {
    return hex(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))));
  }
}
