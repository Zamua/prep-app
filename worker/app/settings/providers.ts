// The BYOK provider catalogue: labels, accepted key shapes, console URLs.
// Static metadata the settings page renders; no adapter is imported to
// draw a section.
//
// `claude-subscription` is retired (docs/CELLD-REWRITE.md 7.4): it is not
// offered, but a row migrated from the Python app still renders so its
// owner can delete it and paste an API key instead.

export type ProviderId = 'anthropic-api' | 'openai-api' | 'openrouter-api' | 'claude-subscription';

export interface ProviderInfo {
  provider: ProviderId;
  label: string;
  short_label: string;
  key_prefixes: readonly string[];
  console_url: string;
  default_model: string;
}

export const RETIRED_PROVIDER: ProviderId = 'claude-subscription';

const INFO: Record<ProviderId, ProviderInfo> = {
  'anthropic-api': {
    provider: 'anthropic-api',
    label: 'Anthropic',
    short_label: 'anthropic',
    key_prefixes: ['sk-ant-api03-'],
    console_url: 'https://console.anthropic.com/settings/keys',
    default_model: 'claude-sonnet-4-6',
  },
  'openai-api': {
    provider: 'openai-api',
    label: 'OpenAI',
    short_label: 'openai',
    // OpenAI emits several shapes, all `sk-`; the prefixes other
    // providers claim are rejected before this one is consulted.
    key_prefixes: ['sk-'],
    console_url: 'https://platform.openai.com/api-keys',
    default_model: 'gpt-5-mini',
  },
  'openrouter-api': {
    provider: 'openrouter-api',
    label: 'OpenRouter',
    short_label: 'openrouter',
    key_prefixes: ['sk-or-v1-'],
    console_url: 'https://openrouter.ai/keys',
    default_model: 'anthropic/claude-sonnet-4.5',
  },
  'claude-subscription': {
    provider: 'claude-subscription',
    label: 'Claude subscription',
    short_label: 'claude-sub',
    key_prefixes: ['sk-ant-oat01-'],
    console_url: 'https://docs.claude.com/en/docs/agent-sdk/auth#claude-app-tokens',
    default_model: 'claude-sonnet-4-6',
  },
};

/** Display order on /settings/agent. */
export const PROVIDERS: readonly ProviderId[] = ['anthropic-api', 'openai-api', 'openrouter-api'];

export function providerInfo(id: string): ProviderInfo | null {
  return Object.hasOwn(INFO, id) ? INFO[id as ProviderId] : null;
}

/** A key's provider, most-specific prefix first, or null. */
export function providerForKey(secret: string): ProviderId | null {
  const s = (secret || '').trim();
  if (!s) return null;
  for (const id of ['anthropic-api', 'openrouter-api', 'openai-api'] as const) {
    for (const prefix of INFO[id].key_prefixes) {
      if (!s.startsWith(prefix)) continue;
      if (id === 'openai-api' && (s.startsWith('sk-ant-') || s.startsWith('sk-or-'))) continue;
      return id;
    }
  }
  return null;
}
