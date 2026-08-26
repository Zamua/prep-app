// The adapters behind `AgentPort`: which credential is picked, what each one
// puts on the wire, and which error class every refusal shape becomes.
//
// Two oracles. The parity LLM stub keys a canned answer on the sha256 of the
// request's `messages` alone, so a fixture HIT is proof the adapter's envelope
// is byte for byte what the Python app sent. The taxonomy is checked against
// prep/agent/openai_compat.py and prep/agent/anthropic_api.py, whose messages
// reach the user through a job's error.
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { agentConfig, ANON_NO_AGENT, BYOK_UNUSABLE, FREE_TIER_MAX_CARDS_PER_CALL, fundingTier, NO_FUNDING, requireFundedWorkflow } from '../app/agent/funding.js';
import { AgentBudgetExhausted, AgentBusy, AgentTimeout, AgentUnavailable, type AgentConfig, type Cipher, type UserRepos } from '../app/ports.js';
import { AnthropicAgent } from '../runtime/adapters/agents/anthropic.js';
import { byokAgent, BYOK_MAX_OUTPUT_TOKENS, UnsupportedProvider } from '../runtime/adapters/agents/byok.js';
import { FREE_TIER_MAX_OUTPUT_TOKENS, FreeTierAgent, freeTierConfig, INSTANT_MAX_OUTPUT_TOKENS } from '../runtime/adapters/agents/freeTier.js';
import { InvalidKeyShape, messagesFor, OpenAICompatAgent, scrubExtraBody } from '../runtime/adapters/agents/openaiCompat.js';
import { agentFor, DEFAULT_TIMEOUT_MS, RefusingAgent, SelectedAgent } from '../runtime/adapters/agents/select.js';

const REPO = new URL('../..', import.meta.url).pathname;
const LLM_FIXTURES = join(REPO, 'tests', 'fixtures', 'parity', 'llm');

const FREE_ENV = {
  PREP_FREE_INFERENCE_BASE_URL: 'https://inference.example/v1',
  PREP_FREE_INFERENCE_API_KEY: 'free-key',
  PREP_FREE_INFERENCE_MODEL: 'parity-model',
};

// ---- fakes ------------------------------------------------------------------

interface RepoOpts {
  anonymous?: boolean;
  providers?: string[];
  active?: string | null;
  secrets?: Record<string, string>;
  listThrows?: boolean;
}

function fakeRepos(opts: RepoOpts = {}): UserRepos {
  const providers = opts.providers ?? [];
  return {
    byok: {
      listProviders: () => {
        if (opts.listThrows) throw new Error('unreadable');
        return providers;
      },
      getCiphertext: (p: string) => (opts.secrets ?? {})[p] ?? null,
    },
    prefs: {
      get: () => ({ is_anonymous: opts.anonymous ? 1 : 0 }),
      getActiveByokProvider: () => opts.active ?? null,
    },
  } as unknown as UserRepos;
}

const plainCipher: Cipher = { encrypt: async (s) => s, decrypt: async (s) => s };
const brokenCipher: Cipher = { encrypt: async (s) => s, decrypt: async () => { throw new Error('bad key'); } };

interface Sent {
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}

/** Answers every outbound call with `respond`, recording what was sent. */
function captureFetch(respond: () => Response | Promise<Response> | never): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    sent.push({ url, headers: (init?.headers ?? {}) as Record<string, string>, body: JSON.parse(String(init?.body ?? '{}')) });
    return respond();
  });
  return sent;
}

const json = (status: number, payload: unknown): Response => Response.json(payload as object, { status });
const ok = (content: string): Response => json(200, { choices: [{ message: { content } }] });

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---- the free tier's configuration ------------------------------------------

describe('the shared tier is configured or it is off', () => {
  it('is off, silently, when nothing is set', () => {
    const warn = vi.fn();
    expect(freeTierConfig({}, { warn })).toBeNull();
    expect(warn).not.toHaveBeenCalled();
  });

  it('is off, loudly, when it is half-configured', () => {
    const warn = vi.fn();
    expect(freeTierConfig({ PREP_FREE_INFERENCE_BASE_URL: 'https://x/v1', PREP_FREE_INFERENCE_MODEL: 'm' }, { warn })).toBeNull();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('PREP_FREE_INFERENCE_API_KEY missing'));
  });

  it('refuses an extra body that is not a JSON object', () => {
    const warn = vi.fn();
    expect(freeTierConfig({ ...FREE_ENV, PREP_FREE_INFERENCE_EXTRA_BODY: '[]' }, { warn })).toBeNull();
    expect(freeTierConfig({ ...FREE_ENV, PREP_FREE_INFERENCE_EXTRA_BODY: '{' }, { warn })).toBeNull();
    expect(warn).toHaveBeenCalledTimes(2);
  });

  it('strips the base URL trailing slash and defaults the job-sized cap', () => {
    const config = freeTierConfig({ ...FREE_ENV, PREP_FREE_INFERENCE_BASE_URL: 'https://inference.example/v1//' })!;
    expect(config.baseUrl).toBe('https://inference.example/v1');
    expect(config.maxTokens).toBe(FREE_TIER_MAX_OUTPUT_TOKENS);
    expect(config.shared).toBe(true);
    expect(config.label).toBe('free AI');
  });

  it("caps instant output lower than a job's", () => {
    expect(INSTANT_MAX_OUTPUT_TOKENS).toBe(1024);
    expect(FREE_TIER_MAX_OUTPUT_TOKENS).toBe(32768);
    expect(freeTierConfig(FREE_ENV, { maxTokens: INSTANT_MAX_OUTPUT_TOKENS })!.maxTokens).toBe(1024);
  });
});

// ---- what goes on the wire ---------------------------------------------------

describe('the chat-completions body', () => {
  const agent = () => new FreeTierAgent(freeTierConfig(FREE_ENV)!);

  it('carries one user message, the model and the cap', async () => {
    const sent = captureFetch(() => ok('answer'));
    expect(await agent().complete({ system: '', user: 'hello' })).toBe('answer');
    expect(sent).toHaveLength(1);
    expect(sent[0]!.url).toBe('https://inference.example/v1/chat/completions');
    expect(sent[0]!.body).toEqual({ model: 'parity-model', max_tokens: 32768, messages: [{ role: 'user', content: 'hello' }] });
    expect(sent[0]!.headers['Authorization']).toBe('Bearer free-key');
  });

  it('never lets a deploy knob claim the conversation or the cap', () => {
    const warn = vi.fn();
    expect(scrubExtraBody({ messages: [], max_tokens: 1, temperature: 0 }, warn, 'free AI')).toEqual({ temperature: 0 });
    expect(warn).toHaveBeenCalledTimes(2);
  });

  it('merges the deploy knobs it does allow', async () => {
    const config = freeTierConfig({ ...FREE_ENV, PREP_FREE_INFERENCE_EXTRA_BODY: '{"chat_template_kwargs": {"enable_thinking": false}}' })!;
    const sent = captureFetch(() => ok('answer'));
    await new FreeTierAgent(config).complete({ system: '', user: 'hi' });
    expect(sent[0]!.body['chat_template_kwargs']).toEqual({ enable_thinking: false });
  });

  it('adds a system turn only when one is asked for', () => {
    expect(messagesFor({ system: '', user: 'u' })).toEqual([{ role: 'user', content: 'u' }]);
    expect(messagesFor({ system: 's', user: 'u' })).toEqual([
      { role: 'system', content: 's' },
      { role: 'user', content: 'u' },
    ]);
  });

  it('sends OpenRouter its attribution headers and each provider its model', async () => {
    const sent = captureFetch(() => ok('answer'));
    await byokAgent('openrouter-api', 'sk-or-v1-key', { timeoutMs: 1000 }).complete({ system: '', user: 'hi' });
    expect(sent[0]!.url).toBe('https://openrouter.ai/api/v1/chat/completions');
    expect(sent[0]!.headers['HTTP-Referer']).toBe('https://prepcards.app');
    expect(sent[0]!.headers['X-Title']).toBe('prep');
    expect(sent[0]!.body['model']).toBe('anthropic/claude-sonnet-4.5');
    expect(sent[0]!.body['max_tokens']).toBe(BYOK_MAX_OUTPUT_TOKENS);
  });

  it('posts an anthropic key to the messages API, not a chat-completions one', async () => {
    const sent = captureFetch(() => json(200, { content: [{ type: 'text', text: ' verdict ' }] }));
    expect(await byokAgent('anthropic-api', 'sk-ant-api03-key', { timeoutMs: 1000 }).complete({ system: '', user: 'hi' })).toBe('verdict');
    expect(sent[0]!.url).toBe('https://api.anthropic.com/v1/messages');
    expect(sent[0]!.headers['x-api-key']).toBe('sk-ant-api03-key');
    expect(sent[0]!.headers['anthropic-version']).toBe('2023-06-01');
    expect(sent[0]!.body).toEqual({ model: 'claude-sonnet-4-6', max_tokens: BYOK_MAX_OUTPUT_TOKENS, messages: [{ role: 'user', content: 'hi' }] });
  });

  it("refuses a key whose shape is not the provider's", () => {
    expect(() => byokAgent('anthropic-api', 'sk-or-v1-nope', { timeoutMs: 1 })).toThrow(InvalidKeyShape);
    expect(() => byokAgent('openrouter-api', 'sk-nope', { timeoutMs: 1 })).toThrow(InvalidKeyShape);
    expect(() => byokAgent('claude-subscription', 'sk-ant-oat01-x', { timeoutMs: 1 })).toThrow(UnsupportedProvider);
  });
});

// ---- the taxonomy ------------------------------------------------------------

const SHARED = () => new FreeTierAgent(freeTierConfig(FREE_ENV)!);
const OWN = () => byokAgent('openai-api', 'sk-own-key', { timeoutMs: DEFAULT_TIMEOUT_MS });

async function refusal(agent: { complete(r: { system: string; user: string }): Promise<string> }): Promise<Error> {
  try {
    await agent.complete({ system: '', user: 'p' });
  } catch (e) {
    return e as Error;
  }
  throw new Error('expected a refusal');
}

describe("a shared credential and an owner's own key refuse differently", () => {
  it('reads a 429 as contention on the shared tier', async () => {
    captureFetch(() => json(429, { error: { message: 'rate limited' } }));
    const e = await refusal(SHARED());
    expect(e).toBeInstanceOf(AgentBusy);
    expect(e).not.toBeInstanceOf(AgentTimeout);
    expect(e.message).toBe("free AI is busy right now (it's shared by everyone on this deploy): rate limited; try again later, or add your own key in Settings for dedicated capacity");
  });

  it("reads the same 429 as the owner's own budget", async () => {
    captureFetch(() => json(429, { error: { message: 'rate limited' } }));
    const e = await refusal(OWN());
    expect(e).toBeInstanceOf(AgentBudgetExhausted);
    expect(e).not.toBeInstanceOf(AgentBusy);
    expect(e.message).toBe('OpenAI API quota/rate exhausted: rate limited');
  });

  it('reads a quota-coded body at any status the same way', async () => {
    captureFetch(() => json(200, { error: { message: 'no credit', code: 'insufficient_quota' } }));
    expect(await refusal(SHARED())).toBeInstanceOf(AgentBusy);
    vi.unstubAllGlobals();
    captureFetch(() => json(200, { error: { message: 'no credit', type: 'insufficient_quota' } }));
    expect(await refusal(OWN())).toBeInstanceOf(AgentBudgetExhausted);
  });

  it('never treats an auth failure as a budget', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
    captureFetch(() => json(401, { error: { message: 'bad key' } }));
    const e = await refusal(SHARED());
    expect(e).toBeInstanceOf(AgentUnavailable);
    expect(e).not.toBeInstanceOf(AgentBusy);
    expect(e.message).toBe('free AI API auth rejected: bad key');
    // A rejected deploy-wide key is operator-actionable, so it is logged.
    expect(errors).toHaveBeenCalledWith(expect.stringContaining('rejected the deploy-wide shared key (HTTP 401)'));
    errors.mockRestore();
  });

  it("counts a shared timeout as spend and an owner's as plain unavailability", async () => {
    captureFetch(() => {
      throw Object.assign(new Error('timed out'), { name: 'TimeoutError' });
    });
    const shared = await refusal(SHARED());
    expect(shared).toBeInstanceOf(AgentTimeout);
    expect(shared).toBeInstanceOf(AgentBusy);
    expect(shared.message).toBe('free AI timed out after 60.0s; try again later, or add your own key in Settings for dedicated capacity');
    const own = await refusal(OWN());
    expect(own).toBeInstanceOf(AgentUnavailable);
    expect(own).not.toBeInstanceOf(AgentBusy);
    expect(own.message).toBe('OpenAI API timeout after 120.0s');
  });

  it('reads a transport failure as neither busy nor budget', async () => {
    captureFetch(() => {
      throw new TypeError('connection refused');
    });
    const e = await refusal(SHARED());
    expect(e).toBeInstanceOf(AgentUnavailable);
    expect(e).not.toBeInstanceOf(AgentBusy);
    expect(e.message).toBe('free AI API transport error: connection refused');
  });

  it('caps the upstream message and falls back to the status', async () => {
    captureFetch(() => json(500, { error: { message: 'x'.repeat(400) } }));
    expect((await refusal(SHARED())).message).toBe(`free AI API error (500): ${'x'.repeat(200)}`);
    vi.unstubAllGlobals();
    captureFetch(() => json(500, {}));
    expect((await refusal(SHARED())).message).toBe('free AI API error (500): HTTP 500');
  });

  it('refuses an answer with no text', async () => {
    captureFetch(() => ok('   '));
    expect((await refusal(SHARED())).message).toBe('free AI API returned no text content');
    vi.unstubAllGlobals();
    captureFetch(() => new Response('not json', { status: 200 }));
    expect((await refusal(SHARED())).message).toContain('free AI API returned non-JSON body:');
  });

  it("maps anthropic's own budget error types", async () => {
    for (const type of ['rate_limit_error', 'credit_balance_too_low', 'billing_error']) {
      vi.unstubAllGlobals();
      captureFetch(() => json(400, { error: { type, message: 'out of credit' } }));
      const e = await refusal(new AnthropicAgent({ apiKey: 'sk-ant-api03-k', model: 'm', maxTokens: 8, timeoutMs: 1000 }));
      expect(e, type).toBeInstanceOf(AgentBudgetExhausted);
      expect(e.message).toBe('anthropic API budget/rate exhausted: out of credit');
    }
    vi.unstubAllGlobals();
    captureFetch(() => json(403, { error: { type: 'permission_error', message: 'nope' } }));
    const auth = await refusal(new AnthropicAgent({ apiKey: 'sk-ant-api03-k', model: 'm', maxTokens: 8, timeoutMs: 1000 }));
    expect(auth).toBeInstanceOf(AgentUnavailable);
    expect(auth).not.toBeInstanceOf(AgentBudgetExhausted);
    expect(auth.message).toBe('anthropic API auth rejected: nope');
  });

  it('joins anthropic text blocks and ignores the others', async () => {
    captureFetch(() => json(200, { content: [{ type: 'thinking', text: 'no' }, { type: 'text', text: 'a' }, { type: 'text', text: 'b' }] }));
    expect(await new AnthropicAgent({ apiKey: 'sk-ant-api03-k', model: 'm', maxTokens: 8, timeoutMs: 1000 }).complete({ system: '', user: 'p' })).toBe('ab');
  });
});

// ---- selection ---------------------------------------------------------------

describe('which credential funds a call', () => {
  const deps = { env: FREE_ENV, cipher: plainCipher, warn: () => {} };

  it('answers the tier from row existence alone', () => {
    expect(fundingTier(fakeRepos({ providers: ['openai-api'] }), true)).toBe('byok');
    expect(fundingTier(fakeRepos(), true)).toBe('free');
    expect(fundingTier(fakeRepos(), false)).toBe('none');
    expect(fundingTier(fakeRepos({ anonymous: true }), true)).toBe('none');
    // Fail closed: an unreadable row set must never be served a shared key.
    expect(fundingTier(fakeRepos({ listThrows: true }), true)).toBe('byok');
  });

  it('refuses a start no tier would fund', () => {
    expect(() => requireFundedWorkflow(fakeRepos(), false)).toThrow(NO_FUNDING);
    expect(() => requireFundedWorkflow(fakeRepos(), true)).not.toThrow();
  });

  it("caps a shared-tier generation and leaves an owner's own key alone", () => {
    expect(FREE_TIER_MAX_CARDS_PER_CALL).toBe(5);
  });

  it("prefers the owner's own key over the shared tier", () => {
    const config = agentConfig(fakeRepos({ providers: ['openai-api'], secrets: { 'openai-api': 'cipher' } }), true);
    expect(config).toEqual({ tier: 'byok', provider: 'openai-api', ciphertext: 'cipher' });
  });

  it('honours the active provider, then the built-in order', () => {
    const secrets = { 'openai-api': 'o', 'anthropic-api': 'a', 'openrouter-api': 'r' };
    const providers = Object.keys(secrets);
    expect(agentConfig(fakeRepos({ providers, secrets, active: 'openai-api' }), true)).toMatchObject({ provider: 'openai-api' });
    expect(agentConfig(fakeRepos({ providers, secrets }), true)).toMatchObject({ provider: 'anthropic-api' });
    // A stale active value is skipped rather than honoured.
    expect(agentConfig(fakeRepos({ providers, secrets, active: 'gone' }), true)).toMatchObject({ provider: 'anthropic-api' });
  });

  it('never falls through to the shared tier when a row exists but yields nothing', () => {
    // A retired claude-subscription row is exactly this shape.
    expect(agentConfig(fakeRepos({ providers: ['claude-subscription'] }), true)).toEqual({ tier: 'none', reason: BYOK_UNUSABLE });
  });

  it('funds no guest', () => {
    expect(agentConfig(fakeRepos({ anonymous: true }), true)).toEqual({ tier: 'none', reason: ANON_NO_AGENT });
  });

  it('answers the shared tier only when the owner brought nothing', () => {
    expect(agentConfig(fakeRepos(), true)).toEqual({ tier: 'free' });
    expect(agentConfig(fakeRepos(), false)).toEqual({ tier: 'none', reason: NO_FUNDING });
  });

  it('builds the adapter the config names', async () => {
    expect(await agentFor({ tier: 'free' }, deps)).toBeInstanceOf(FreeTierAgent);
    expect(await agentFor({ tier: 'free' }, { ...deps, env: {} })).toBeInstanceOf(RefusingAgent);
    expect(await agentFor({ tier: 'byok', provider: 'openai-api', ciphertext: 'sk-plain' }, deps)).toBeInstanceOf(OpenAICompatAgent);
    expect(await agentFor({ tier: 'byok', provider: 'anthropic-api', ciphertext: 'sk-ant-api03-x' }, deps)).toBeInstanceOf(AnthropicAgent);
  });

  it('refuses rather than downgrades when the key cannot be read', async () => {
    for (const bad of [
      { tier: 'byok', provider: 'openai-api', ciphertext: 'sk-x' } as AgentConfig,
      { tier: 'byok', provider: 'openai-api', ciphertext: 'not-a-key-shape' } as AgentConfig,
    ]) {
      const cipher = bad.tier === 'byok' && bad.ciphertext === 'sk-x' ? brokenCipher : plainCipher;
      const agent = await agentFor(bad, { ...deps, cipher });
      expect(agent).toBeInstanceOf(RefusingAgent);
      await expect(agent.complete({ system: '', user: 'p' })).rejects.toThrow(BYOK_UNUSABLE);
    }
    const noKey = await agentFor({ tier: 'byok', provider: 'openai-api', ciphertext: 'sk-x' }, { ...deps, cipher: null });
    await expect(noKey.complete({ system: '', user: 'p' })).rejects.toThrow(BYOK_UNUSABLE);
  });

  it('re-reads the credential on every call, so a revoked key stops the next step', async () => {
    const sent = captureFetch(() => ok('answer'));
    let config: AgentConfig = { tier: 'byok', provider: 'openai-api', ciphertext: 'sk-first' };
    const reads: number[] = [];
    const agent = new SelectedAgent(() => {
      reads.push(reads.length);
      return config;
    }, deps);
    await agent.complete({ system: '', user: 'one' });
    config = { tier: 'none', reason: BYOK_UNUSABLE };
    await expect(agent.complete({ system: '', user: 'two' })).rejects.toThrow(BYOK_UNUSABLE);
    expect(reads).toHaveLength(2);
    expect(sent).toHaveLength(1);
    expect(sent[0]!.headers['Authorization']).toBe('Bearer sk-first');
  });

  it("bounds a call by the caller's deadline as well as its own", async () => {
    vi.stubGlobal('fetch', async (_input: RequestInfo | URL, init?: RequestInit) => {
      await new Promise((resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        setTimeout(resolve, 5_000);
      });
      return ok('never');
    });
    const agent = new FreeTierAgent(freeTierConfig(FREE_ENV, { timeoutMs: 60_000 })!);
    const e = await (async () => {
      try {
        await agent.complete({ system: '', user: 'p', signal: AbortSignal.timeout(30) });
      } catch (err) {
        return err as Error;
      }
      throw new Error('expected a refusal');
    })();
    expect(e).toBeInstanceOf(AgentTimeout);
  });
});

// ---- the stub -----------------------------------------------------------------

interface Stub {
  origin: string;
  baseUrl: string;
  proc: ChildProcessWithoutNullStreams;
}

async function bootStub(fixtures: string): Promise<Stub> {
  const proc = spawn(join(REPO, '.venv', 'bin', 'python'), ['-m', 'tests.parity.llm_stub', '--port', '0', '--fixtures', fixtures], { cwd: REPO });
  const line = await new Promise<string>((resolve, reject) => {
    const rl = createInterface({ input: proc.stdout });
    rl.once('line', resolve);
    proc.once('error', reject);
    setTimeout(() => reject(new Error('the stub did not announce a port')), 15_000);
  });
  const baseUrl = /parity llm stub: (\S+)/.exec(line)?.[1];
  if (!baseUrl) throw new Error(`unexpected stub banner: ${line}`);
  return { origin: baseUrl.replace(/\/v1$/, ''), baseUrl, proc };
}

const control = (stub: Stub, path: string, payload?: unknown): Promise<Response> =>
  fetch(`${stub.origin}/_control/${path}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: payload === undefined ? undefined : JSON.stringify(payload) });

const read = async <T>(stub: Stub, path: string): Promise<T> => (await (await fetch(`${stub.origin}/_control/${path}`)).json()) as T;

function stubAgent(stub: Stub, opts: { timeoutMs?: number } = {}): FreeTierAgent {
  return new FreeTierAgent(freeTierConfig({ ...FREE_ENV, PREP_FREE_INFERENCE_BASE_URL: stub.baseUrl }, { timeoutMs: opts.timeoutMs })!);
}

/** The stub's own key: sha256 over the canonical `messages` list. */
function fixtureKey(messages: readonly { role: string; content: string }[]): string {
  const canonical = `[${messages.map((m) => `{"content":${JSON.stringify(m.content)},"role":${JSON.stringify(m.role)}}`).join(',')}]`;
  return createHash('sha256').update(canonical, 'utf8').digest('hex');
}

interface Fixture {
  key: string;
  messages: { role: string; content: string }[];
  body: string;
}

const fixtures: Fixture[] = readdirSync(LLM_FIXTURES)
  .filter((f) => f.endsWith('.json'))
  .map((f) => JSON.parse(readFileSync(join(LLM_FIXTURES, f), 'utf8')) as Fixture);

describe('the recorded prompts replay through the adapter', () => {
  let stub: Stub;
  let scratch: Stub;

  beforeAll(async () => {
    vi.unstubAllGlobals();
    stub = await bootStub(LLM_FIXTURES);
    // A miss writes a note beside the fixtures, so the error mode gets its
    // own directory rather than polluting the corpus.
    scratch = await bootStub(mkdtempSync(join(tmpdir(), 'llm-stub-')));
  });

  afterAll(() => {
    stub?.proc.kill();
    scratch?.proc.kill();
  });

  it('has a corpus to replay', () => {
    expect(fixtures.length).toBeGreaterThan(0);
  });

  // The stub keys on `messages` alone, so a hit is byte-for-byte proof that
  // the envelope this adapter sends is the one the Python app sent. Each of
  // the four job kinds joins the corpus as lane E records it; nothing here
  // changes when it does.
  it.each(fixtures.map((f) => [f.key.slice(0, 16), f] as const))('replays %s', async (_name, fixture) => {
    expect(fixture.messages).toHaveLength(1);
    expect(fixture.messages[0]!.role).toBe('user');
    expect(fixtureKey(messagesFor({ system: '', user: fixture.messages[0]!.content }))).toBe(fixture.key);

    const text = await stubAgent(stub).complete({ system: '', user: fixture.messages[0]!.content });
    expect(text).toBe(String((JSON.parse(fixture.body) as { choices: { message: { content: string } }[] }).choices[0]!.message.content).trim());
    expect((await read<{ keys: string[] }>(stub, 'requests')).keys).toContain(fixture.key);
  });

  it('holds a call open until it is released', async () => {
    await control(scratch, 'canned', { content: 'released' });
    await control(scratch, 'hold');
    const inFlight = stubAgent(scratch).complete({ system: '', user: 'held prompt' });
    let settled = false;
    void inFlight.then(
      () => (settled = true),
      () => (settled = true),
    );
    await new Promise((r) => setTimeout(r, 200));
    expect(settled).toBe(false);
    expect((await read<{ count: number }>(scratch, 'held')).count).toBe(1);
    await control(scratch, 'release');
    expect(await inFlight).toBe('released');
    await control(scratch, 'canned', { content: null });
  });

  it('reads a held call past the deadline as spend on the shared tier', async () => {
    await control(scratch, 'hold');
    try {
      const e = await refusal(stubAgent(scratch, { timeoutMs: 300 }));
      expect(e).toBeInstanceOf(AgentTimeout);
      expect(e.message).toBe('free AI timed out after 0.3s; try again later, or add your own key in Settings for dedicated capacity');
    } finally {
      await control(scratch, 'release');
    }
  });

  it("reads an unrecorded prompt as the stub's error", async () => {
    const e = await refusal(stubAgent(scratch));
    expect(e).toBeInstanceOf(AgentUnavailable);
    expect(e).not.toBeInstanceOf(AgentBusy);
    expect(e.message).toMatch(/^free AI API error \(404\): no fixture for [0-9a-f]{64}$/);
  });

  it("reads the stub's canned answer through the OpenAI shape", async () => {
    await control(scratch, 'canned', { content: '  canned answer  ' });
    expect(await stubAgent(scratch).complete({ system: '', user: 'anything' })).toBe('canned answer');
    await control(scratch, 'canned', { content: null });
  });
});
