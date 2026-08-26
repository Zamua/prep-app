import { describe, expect, it, vi } from 'vitest';
import { b64uEncode } from '../domain/base64';
import { KEYS_URL, KeyExchangeFailed, OpenRouterOAuth, VERIFIER_BYTES } from '../runtime/adapters/openrouter';
import { SeededRandom, WebCryptoRandom } from '../runtime/adapters/random';

const counting = (fill: number) => ({
  bytes: (n: number) => new Uint8Array(n).fill(fill),
  choice: <T,>(seq: readonly T[]): T => seq[0]!,
});

async function sha256B64u(text: string): Promise<string> {
  return b64uEncode(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text) as unknown as BufferSource)));
}

describe('the PKCE challenge', () => {
  it('is S256 over a 32-byte base64url verifier', async () => {
    const { verifier, challenge } = await new OpenRouterOAuth(counting(7)).startChallenge();
    expect(verifier).toBe(b64uEncode(new Uint8Array(VERIFIER_BYTES).fill(7)));
    expect(verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(challenge).toBe(await sha256B64u(verifier));
    expect(challenge).not.toBe(verifier);
  });

  it('draws a fresh verifier per start', async () => {
    const auth = new OpenRouterOAuth(new WebCryptoRandom());
    const seen = new Set<string>();
    for (let i = 0; i < 5; i++) seen.add((await auth.startChallenge()).verifier);
    expect(seen.size).toBe(5);
  });

  it('is reproducible under the parity generator, so a recorded flow replays', async () => {
    const first = await new OpenRouterOAuth(new SeededRandom(20260314)).startChallenge();
    const again = await new OpenRouterOAuth(new SeededRandom(20260314)).startChallenge();
    expect(again).toEqual(first);
  });
});

describe('the key exchange', () => {
  const okFetch = (body: unknown, status = 200) =>
    vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })) as unknown as typeof fetch;

  it('posts the code and the verifier and returns the minted key', async () => {
    const f = okFetch({ key: '  sk-or-v1-minted  ' });
    const key = await new OpenRouterOAuth(counting(1), f).exchange('the-code', 'the-verifier');
    expect(key).toBe('sk-or-v1-minted');
    const [url, init] = (f as unknown as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe(KEYS_URL);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ code: 'the-code', code_verifier: 'the-verifier', code_challenge_method: 'S256' });
  });

  it('names what went wrong instead of storing nothing quietly', async () => {
    const auth = (f: typeof fetch) => new OpenRouterOAuth(counting(1), f);
    await expect(auth(okFetch({ error: 'bad code' }, 400)).exchange('c', 'v')).rejects.toThrow(KeyExchangeFailed);
    await expect(auth(okFetch({ error: 'bad code' }, 400)).exchange('c', 'v')).rejects.toThrow(/HTTP 400/);
    await expect(auth(okFetch({}, 200)).exchange('c', 'v')).rejects.toThrow(/no key/);
    await expect(auth(okFetch({ key: '   ' }, 200)).exchange('c', 'v')).rejects.toThrow(/no key/);
    const notJson = (async () => new Response('<html>', { status: 200 })) as unknown as typeof fetch;
    await expect(auth(notJson).exchange('c', 'v')).rejects.toThrow(/not JSON/);
    const offline = (async () => {
      throw new Error('connect ETIMEDOUT');
    }) as unknown as typeof fetch;
    await expect(auth(offline).exchange('c', 'v')).rejects.toThrow(/couldn't reach OpenRouter/);
  });

  it('never carries the verifier anywhere but the request body', async () => {
    const f = okFetch({ key: 'sk-or-v1-x' });
    await new OpenRouterOAuth(counting(1), f).exchange('the-code', 'secret-verifier');
    const [url, init] = (f as unknown as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(String(url)).not.toContain('secret-verifier');
    expect(JSON.stringify(init.headers)).not.toContain('secret-verifier');
  });
});
