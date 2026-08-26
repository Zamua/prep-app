// The deploy's shared free tier as an `AgentPort`: one OpenAI-compatible
// chat-completions call. Configured only when base URL, key and model are
// all set, so a half-configured deploy has no free tier rather than a
// broken one.
import { AgentUnavailable, type AgentPort, type AgentRequest } from '../../app/ports.js';
import { InstantBusy } from '../../app/instant/generate.js';

export interface FreeTierEnv {
  PREP_FREE_INFERENCE_BASE_URL?: string;
  PREP_FREE_INFERENCE_API_KEY?: string;
  PREP_FREE_INFERENCE_MODEL?: string;
  CELLD_FETCH_TIMEOUT_S?: string;
}

export interface FreeTierConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
  maxTokens: number;
  timeoutMs: number;
}

/** The instant endpoint's own output cap; the deck-wide jobs raise it. */
export const INSTANT_MAX_OUTPUT_TOKENS = 1024;
export const DEFAULT_TIMEOUT_MS = 60_000;

export function freeTierConfig(env: FreeTierEnv, opts: { maxTokens?: number; timeoutMs?: number } = {}): FreeTierConfig | null {
  const baseUrl = (env.PREP_FREE_INFERENCE_BASE_URL ?? '').trim();
  const apiKey = (env.PREP_FREE_INFERENCE_API_KEY ?? '').trim();
  const model = (env.PREP_FREE_INFERENCE_MODEL ?? '').trim();
  if (!baseUrl || !apiKey || !model) return null;
  return {
    baseUrl: baseUrl.replace(/\/+$/, ''),
    apiKey,
    model,
    maxTokens: opts.maxTokens ?? INSTANT_MAX_OUTPUT_TOKENS,
    timeoutMs: opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
  };
}

export class FreeTierAgent implements AgentPort {
  constructor(private readonly config: FreeTierConfig) {}

  async complete(request: AgentRequest): Promise<string> {
    // `messages` is the whole conversation and is never overridable: the
    // caller's output cap is what the abuse arithmetic assumes.
    const body = {
      model: this.config.model,
      max_tokens: request.maxTokens ?? this.config.maxTokens,
      messages: [{ role: 'user', content: request.user }],
    };
    let res: Response;
    try {
      res = await fetch(`${this.config.baseUrl}/chat/completions`, {
        method: 'POST',
        headers: { authorization: `Bearer ${this.config.apiKey}`, 'content-type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(this.config.timeoutMs),
      });
    } catch (e) {
      // The request was issued, so a shared-credential timeout counts as
      // spend; anything else is transport.
      const name = e instanceof Error ? e.name : '';
      if (name === 'TimeoutError' || name === 'AbortError') throw new AgentUnavailable(`free tier timed out after ${this.config.timeoutMs}ms`);
      throw new AgentUnavailable(`free tier transport error: ${e instanceof Error ? e.message : String(e)}`);
    }
    if (res.status === 429 || res.status === 503) throw new InstantBusy(`free tier is busy (HTTP ${res.status})`);
    if (!res.ok) throw new AgentUnavailable(`free tier returned HTTP ${res.status}`);
    let data: unknown;
    try {
      data = await res.json();
    } catch (e) {
      throw new AgentUnavailable(`free tier returned a non-JSON body: ${e instanceof Error ? e.message : String(e)}`);
    }
    const payload = data as { choices?: { message?: { content?: string } }[]; error?: unknown };
    if (payload && payload.error) throw new InstantBusy('free tier reported an error');
    const text = (payload?.choices?.[0]?.message?.content ?? '').trim();
    if (!text) throw new AgentUnavailable('free tier returned no text content');
    return text;
  }
}
