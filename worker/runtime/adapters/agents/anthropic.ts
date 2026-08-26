// Anthropic's Messages API with an owner-supplied key, transcribed from
// prep/agent/anthropic_api.py. A different endpoint and a different auth
// header from the OpenAI-compatible ones, so it is its own adapter rather
// than a config of that one.
import { AgentBudgetExhausted, AgentUnavailable, type AgentPort, type AgentRequest } from '../../../app/ports.js';
import { pyRepr } from '../../../domain/grading/pyrepr.js';
import { aborted, deadline, describe, InvalidKeyShape, payloadOf } from './openaiCompat.js';

const API_URL = 'https://api.anthropic.com/v1/messages';
const API_VERSION = '2023-06-01';

/** Anthropic spells credit and quota trouble as an error `type`. */
const BUDGET_ERROR_TYPES = new Set(['rate_limit_error', 'credit_balance_too_low', 'billing_error']);

const MESSAGE_CAP = 200;

export interface AnthropicConfig {
  apiKey: string;
  model: string;
  maxTokens: number;
  timeoutMs: number;
}

export class AnthropicAgent implements AgentPort {
  constructor(private readonly config: AnthropicConfig) {
    // The settings route checks the prefix on store; the adapter's own
    // invariant is that whatever it sends looks like an API key.
    if (!config.apiKey || !config.apiKey.startsWith('sk-ant-')) throw new InvalidKeyShape('invalid Anthropic API key shape');
  }

  async complete(request: AgentRequest): Promise<string> {
    const c = this.config;
    const body: Record<string, unknown> = {
      model: c.model,
      max_tokens: request.maxTokens ?? c.maxTokens,
      messages: [{ role: 'user', content: request.user }],
    };
    if (request.system) body['system'] = request.system;

    let res: Response;
    const timeoutS = c.timeoutMs / 1000;
    try {
      res = await fetch(API_URL, {
        method: 'POST',
        headers: { 'x-api-key': c.apiKey, 'anthropic-version': API_VERSION, 'content-type': 'application/json' },
        body: JSON.stringify(body),
        signal: deadline(request.signal, c.timeoutMs),
      });
    } catch (e) {
      if (aborted(e)) throw new AgentUnavailable(`anthropic API timeout after ${pyRepr(timeoutS)}s`);
      throw new AgentUnavailable(`anthropic API transport error: ${describe(e)}`);
    }

    if (res.status !== 200) raiseForPayload(res.status, await payloadOf(res));

    let data: unknown;
    try {
      data = await res.json();
    } catch (e) {
      throw new AgentUnavailable(`anthropic API returned non-JSON body: ${describe(e)}`);
    }
    const blocks = (data as { content?: unknown }).content;
    if (blocks !== undefined && !Array.isArray(blocks)) throw new AgentUnavailable('anthropic API response shape unexpected: content is not a list');
    // This adapter requests no tool use, so text blocks are all there is.
    const text = (Array.isArray(blocks) ? blocks : [])
      .filter((b): b is { type: string; text?: unknown } => b !== null && typeof b === 'object' && (b as { type?: unknown }).type === 'text')
      .map((b) => (typeof b.text === 'string' ? b.text : ''))
      .join('')
      .trim();
    if (!text) throw new AgentUnavailable('anthropic API returned no text content');
    return text;
  }
}

/** The body is never echoed verbatim: Anthropic sometimes quotes parts of the
 * request back, which would put the prompt in a log line. */
function raiseForPayload(status: number, payload: Record<string, unknown>): never {
  const raw = payload['error'];
  const error: Record<string, unknown> = raw !== null && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
  const type = String(error['type'] ?? '').trim().toLowerCase();
  const msg = (String(error['message'] || `HTTP ${status}`) || `HTTP ${status}`).trim().slice(0, MESSAGE_CAP);

  if (status === 401 || status === 403) throw new AgentUnavailable(`anthropic API auth rejected: ${msg}`);
  if (BUDGET_ERROR_TYPES.has(type) || status === 429) throw new AgentBudgetExhausted(`anthropic API budget/rate exhausted: ${msg}`);
  throw new AgentUnavailable(`anthropic API error (${status}): ${msg}`);
}
