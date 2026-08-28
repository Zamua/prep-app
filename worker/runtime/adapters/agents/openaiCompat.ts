// The de-facto standard chat-completions wire format: one POST, one text
// answer, and the error mapping the refusal taxonomy rests on.
//
// `shared` is the whole mode split: with a deploy-wide credential a 429 or a
// quota-coded body is contention (`AgentBusy`) and a transport timeout is
// `AgentTimeout`; with the caller's own key the same signals are their budget
// (`AgentBudgetExhausted`).
import { AgentBudgetExhausted, AgentBusy, AgentTimeout, AgentUnavailable, type AgentPort, type AgentRequest } from '../../../app/ports.js';
import { literal } from '../../../domain/grading/literal.js';

export interface CompatConfig {
  apiKey: string;
  baseUrl: string;
  model: string;
  /** Deploy knobs merged into the body; never the conversation or the cap. */
  extraBody?: Readonly<Record<string, unknown>> | null;
  shared?: boolean;
  maxTokens: number;
  timeoutMs: number;
  /** How the provider is named in a user-visible message. */
  label: string;
  /** Accepted key prefixes; empty skips the shape check. */
  keyPrefixes?: readonly string[];
  /** Provider-specific headers, e.g. OpenRouter's attribution pair. */
  headers?: Readonly<Record<string, string>>;
}

/** `extra_body` carries deploy knobs only: the caller's cap is what the abuse
 * arithmetic assumes, and the conversation is never a deploy's to set. */
const RESERVED_BODY_KEYS = ['messages', 'max_tokens'] as const;

const MESSAGE_CAP = 200;

export class InvalidKeyShape extends AgentUnavailable {}

export function scrubExtraBody(extra: Readonly<Record<string, unknown>> | null | undefined, warn: (msg: string) => void, label: string): Record<string, unknown> {
  const out: Record<string, unknown> = { ...(extra ?? {}) };
  for (const key of RESERVED_BODY_KEYS) {
    if (key in out) {
      warn(`${label} adapter: extra_body may not override '${key}'; dropping the key`);
      delete out[key];
    }
  }
  return out;
}

export class OpenAICompatAgent implements AgentPort {
  private readonly extraBody: Record<string, unknown>;

  constructor(
    private readonly config: CompatConfig,
    warn: (msg: string) => void = console.warn,
  ) {
    const prefixes = config.keyPrefixes ?? [];
    if (!config.apiKey || (prefixes.length > 0 && !prefixes.some((p) => config.apiKey.startsWith(p)))) {
      throw new InvalidKeyShape(`invalid ${config.label} API key shape`);
    }
    this.extraBody = scrubExtraBody(config.extraBody, warn, config.label);
  }

  async complete(request: AgentRequest): Promise<string> {
    const c = this.config;
    // Adapter defaults, then the deploy's knobs, then the two keys no knob
    // may claim.
    const body: Record<string, unknown> = { model: c.model, max_tokens: request.maxTokens ?? c.maxTokens, ...this.extraBody };
    body['messages'] = messagesFor(request);

    let res: Response;
    const timeoutS = c.timeoutMs / 1000;
    try {
      res = await fetch(`${c.baseUrl}/chat/completions`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${c.apiKey}`, 'Content-Type': 'application/json', ...(c.headers ?? {}) },
        body: JSON.stringify(body),
        signal: deadline(request.signal, c.timeoutMs),
      });
    } catch (e) {
      if (!aborted(e)) throw new AgentUnavailable(`${c.label} API transport error: ${describe(e)}`);
      if (c.shared) {
        // The request went out, so a shared-credential timeout counts as
        // spend. The message is user-visible: it carries the remedy.
        throw new AgentTimeout(`${c.label} timed out after ${literal(timeoutS)}s; try again later, or add your own key in Settings for dedicated capacity`);
      }
      throw new AgentUnavailable(`${c.label} API timeout after ${literal(timeoutS)}s`);
    }

    if (res.status !== 200) this.raiseForPayload(res.status, await payloadOf(res));

    let data: unknown;
    try {
      data = await res.json();
    } catch (e) {
      throw new AgentUnavailable(`${c.label} API returned non-JSON body: ${describe(e)}`);
    }
    const parsed = (data ?? {}) as { choices?: unknown; error?: unknown; usage?: unknown };
    // Some endpoints put an error object, quota-coded ones included, in a 200
    // body; the same mapping has to hold at any status.
    if (parsed && typeof parsed === 'object' && parsed.error) this.raiseForPayload(res.status, parsed as Record<string, unknown>);

    const choices = Array.isArray(parsed.choices) ? parsed.choices : [];
    const message = (choices[0] as { message?: { content?: unknown } } | undefined)?.message ?? {};
    const text = typeof message.content === 'string' ? message.content.trim() : '';
    if (!text) throw new AgentUnavailable(`${c.label} API returned no text content`);
    return text;
  }

  private raiseForPayload(status: number, payload: Record<string, unknown>): never {
    const c = this.config;
    const raw = payload['error'];
    const error: Record<string, unknown> = raw !== null && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, unknown>) : { message: raw === undefined || raw === null ? '' : String(raw) };
    const msg = (String(error['message'] || `HTTP ${status}`) || `HTTP ${status}`).trim().slice(0, MESSAGE_CAP);
    const type = String(error['type'] ?? '').trim().toLowerCase();
    const code = String(error['code'] ?? '').trim().toLowerCase();

    if (status === 401 || status === 403) {
      // A rejected deploy-wide key is operator misconfiguration, not a user
      // problem; it is logged loudly and still refuses.
      if (c.shared) console.error(`${c.label} API rejected the deploy-wide shared key (HTTP ${status}): ${msg}`);
      throw new AgentUnavailable(`${c.label} API auth rejected: ${msg}`);
    }
    if (status === 429 || type.includes('quota') || code.includes('quota')) {
      if (c.shared) {
        throw new AgentBusy(`${c.label} is busy right now (it's shared by everyone on this deploy): ${msg}; try again later, or add your own key in Settings for dedicated capacity`);
      }
      throw new AgentBudgetExhausted(`${c.label} API quota/rate exhausted: ${msg}`);
    }
    throw new AgentUnavailable(`${c.label} API error (${status}): ${msg}`);
  }
}

/** A system turn is sent only when there is one: the canned-LLM fixtures
 * key on the message list, so an empty turn would miss every fixture. */
export function messagesFor(request: AgentRequest): { role: string; content: string }[] {
  const out: { role: string; content: string }[] = [];
  if (request.system) out.push({ role: 'system', content: request.system });
  out.push({ role: 'user', content: request.user });
  return out;
}

export function deadline(caller: AbortSignal | undefined, timeoutMs: number): AbortSignal {
  const own = AbortSignal.timeout(timeoutMs);
  return caller ? AbortSignal.any([caller, own]) : own;
}

export function aborted(e: unknown): boolean {
  const name = e instanceof Error ? e.name : '';
  return name === 'TimeoutError' || name === 'AbortError';
}

export function describe(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** An error body is best-effort: a non-JSON one still has to map. */
export async function payloadOf(res: Response): Promise<Record<string, unknown>> {
  try {
    const data = await res.json();
    return data !== null && typeof data === 'object' && !Array.isArray(data) ? (data as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}
