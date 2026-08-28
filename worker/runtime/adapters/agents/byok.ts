// One owner's own key. The endpoint and the attribution headers are the
// adapter's to know; the model and the accepted key shapes come from the
// catalogue the settings page renders, so the two cannot drift.
import type { ProviderId } from '../../../app/settings/providers.js';
import { providerInfo } from '../../../app/settings/providers.js';
import type { AgentPort } from '../../../app/ports.js';
import { AnthropicAgent } from './anthropic.js';
import { OpenAICompatAgent } from './openaiCompat.js';

/** A response-length cap, not a spend budget: the owner's key, the owner's
 * bill. */
export const BYOK_MAX_OUTPUT_TOKENS = 4096;

export class UnsupportedProvider extends Error {}

/** OpenRouter's recommended attribution pair: prep traffic then shows up
 * under prep in the owner's dashboard. Billing is unaffected. */
const OPENROUTER_HEADERS = { 'HTTP-Referer': 'https://prepcards.app', 'X-Title': 'prep' };

export interface ByokOpts {
  maxTokens?: number;
  timeoutMs: number;
}

export function byokAgent(provider: string, apiKey: string, opts: ByokOpts): AgentPort {
  const info = providerInfo(provider);
  const maxTokens = opts.maxTokens ?? BYOK_MAX_OUTPUT_TOKENS;
  if (!info || !SUPPORTED.includes(provider as ProviderId)) throw new UnsupportedProvider(`unsupported BYOK provider: ${provider}`);
  if (provider === 'anthropic-api') return new AnthropicAgent({ apiKey, model: info.default_model, maxTokens, timeoutMs: opts.timeoutMs });
  const base = provider === 'openai-api' ? 'https://api.openai.com/v1' : 'https://openrouter.ai/api/v1';
  return new OpenAICompatAgent({
    apiKey,
    baseUrl: base,
    model: info.default_model,
    shared: false,
    maxTokens,
    timeoutMs: opts.timeoutMs,
    label: info.label,
    keyPrefixes: info.key_prefixes,
    headers: provider === 'openrouter-api' ? OPENROUTER_HEADERS : undefined,
  });
}

/** The retired subscription provider is absent: a stored row for it selects
 * nothing, which is what makes the owner fail closed rather than fall through
 * to the shared tier. */
const SUPPORTED: readonly ProviderId[] = ['anthropic-api', 'openai-api', 'openrouter-api'];
