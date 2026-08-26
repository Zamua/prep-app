// OpenRouter's OAuth PKCE flow: the app mints the user's key instead of
// asking them to paste one. The verifier never leaves the browser's cookie
// and this request's memory, so an intercepted code buys nothing.
import type { OpenRouterAuth } from '../../app/settings/openrouter.js';
import type { Random } from '../../app/ports.js';
import { b64uEncode } from '../../domain/base64.js';

export const KEYS_URL = 'https://openrouter.ai/api/v1/auth/keys';
export const VERIFIER_BYTES = 32;

export class KeyExchangeFailed extends Error {}

export class OpenRouterOAuth implements OpenRouterAuth {
  constructor(
    private readonly random: Random,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {}

  async startChallenge(): Promise<{ verifier: string; challenge: string }> {
    const verifier = b64uEncode(this.random.bytes(VERIFIER_BYTES));
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier) as unknown as BufferSource);
    return { verifier, challenge: b64uEncode(new Uint8Array(digest)) };
  }

  async exchange(code: string, verifier: string): Promise<string> {
    let res: Response;
    try {
      res = await this.fetchImpl(KEYS_URL, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ code, code_verifier: verifier, code_challenge_method: 'S256' }),
      });
    } catch (e) {
      throw new KeyExchangeFailed(`couldn't reach OpenRouter (${e instanceof Error ? e.message : String(e)})`);
    }
    if (!res.ok) throw new KeyExchangeFailed(`HTTP ${res.status}`);
    let body: { key?: unknown };
    try {
      body = (await res.json()) as typeof body;
    } catch {
      throw new KeyExchangeFailed('the response was not JSON');
    }
    const key = typeof body.key === 'string' ? body.key.trim() : '';
    if (!key) throw new KeyExchangeFailed('the response carried no key');
    return key;
  }
}
