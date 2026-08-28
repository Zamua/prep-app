// /settings/agent: the free-tier callout, the BYOK rows, and the
// OpenRouter PKCE hand-off. One render helper feeds every route so a new
// context field surfaces on all of them at once.
import type { CredentialMetadata } from '../entities.js';
import { notFound } from '../errors.js';
import { page, type PageRequest, type PageResult } from '../pageResult.js';
import type { Cipher, UserRepos } from '../ports.js';
import { PROVIDERS, providerInfo, RETIRED_PROVIDER, type ProviderInfo } from './providers.js';

/** The deploy-wide subscription path is retired (7.4); the panel it drives
 * renders its not-connected state and its POSTs refuse. */
export const AGENT_STATUS = {
  kind: 'unconfigured',
  logged_in: false,
  reason: 'no CLAUDE_CODE_OAUTH_TOKEN — paste a `claude setup-token` value',
} as const;

export const SUBSCRIPTION_RETIRED =
  'Claude subscription tokens are no longer accepted. Add a personal API key on this page instead (Anthropic, OpenAI, or OpenRouter).';

export const MASTER_KEY_MISSING =
  "BYOK isn't available on this deploy — the operator hasn't configured PREP_KEY_ENCRYPTION_SECRET. Ask whoever runs this instance to enable it.";

/** `sk-ant-api03-…x9zT`. Shorter than that is a placeholder, never a
 * recognizable fragment. */
export function maskSecret(secret: string, prefixChars = 14, suffixChars = 4): string {
  const s = secret || '';
  if (s.length <= prefixChars + suffixChars + 1) return '…';
  return `${s.slice(0, prefixChars)}…${s.slice(-suffixChars)}`;
}

export interface ByokSection {
  provider: string;
  info: ProviderInfo;
  metadata: CredentialMetadata | null;
  is_active: boolean;
  /** Migrated from the retired provider: deletion is the only action. */
  retired?: true;
}

/** The rows the page renders, active resolved. An explicit choice wins
 * while its key is still stored; a stale one clears the column. */
export function byokSections(repos: UserRepos): ByokSection[] {
  const sections: ByokSection[] = PROVIDERS.map((id) => ({
    provider: id,
    info: providerInfo(id)!,
    metadata: repos.byok.metadata(id),
    is_active: false,
  }));

  const chosen = repos.prefs.getActiveByokProvider();
  let active = chosen ? sections.findIndex((s) => s.provider === chosen && s.metadata) : -1;
  if (chosen && active < 0) repos.prefs.setActiveByokProvider(null);
  if (active < 0) active = sections.findIndex((s) => s.metadata);
  if (active >= 0) sections[active]!.is_active = true;

  const retired = repos.byok.metadata(RETIRED_PROVIDER);
  if (retired) {
    sections.push({ provider: RETIRED_PROVIDER, info: providerInfo(RETIRED_PROVIDER)!, metadata: retired, is_active: false, retired: true });
  }
  return sections;
}

export interface AgentRender {
  error?: string | null;
  flash?: string | null;
  byok_error?: string | null;
  byok_flash?: string | null;
  status?: number;
}

export function renderAgentSettings(repos: UserRepos, freeTierConfigured: boolean, opts: AgentRender = {}): PageResult {
  return page(
    'settings_agent.html',
    {
      status: AGENT_STATUS,
      error: opts.error ?? null,
      flash: opts.flash ?? null,
      byok_sections: byokSections(repos),
      byok_error: opts.byok_error ?? null,
      byok_flash: opts.byok_flash ?? null,
      free_tier_configured: freeTierConfigured,
    },
    opts.status,
  );
}

/**
 * The two POSTs of the retired subscription panel, which the page still
 * carries. Both refuse: a deploy-wide token would fund every signup's AI from
 * one credit pool, and there is no stored token left for a disconnect to
 * remove. `byok_error`, not `error`, because the panel that renders `error` is
 * collapsed on a multi-user deploy.
 */
export function subscriptionRefusal(repos: UserRepos, freeTierConfigured: boolean): PageResult {
  return renderAgentSettings(repos, freeTierConfigured, { byok_error: SUBSCRIPTION_RETIRED, status: 403 });
}

/** The slug names a provider we still offer, or a stored retired row.
 * Anything else 404s rather than telling a prober what exists. */
function parseProvider(slug: string, repos: UserRepos): ProviderInfo {
  const info = providerInfo(slug);
  if (!info) throw notFound('unknown provider');
  if (info.provider === RETIRED_PROVIDER && !repos.byok.metadata(RETIRED_PROVIDER)) throw notFound('unknown provider');
  return info;
}

export async function byokConnect(
  repos: UserRepos,
  req: PageRequest,
  deps: { freeTierConfigured: boolean; cipher: Cipher | null },
): Promise<PageResult> {
  const info = parseProvider(req.params['provider'] ?? '', repos);
  const render = (o: AgentRender) => renderAgentSettings(repos, deps.freeTierConfigured, o);
  if (info.provider === RETIRED_PROVIDER) return render({ byok_error: SUBSCRIPTION_RETIRED, status: 400 });

  const secret = (req.form.get('api_key') ?? '').trim();
  if (!secret) return render({ byok_error: 'API key is required.', status: 400 });
  if (!info.key_prefixes.some((p) => secret.startsWith(p))) {
    return render({
      byok_error:
        `That doesn't look like a ${info.label} key — expected one starting with '${info.key_prefixes[0]}'. ` +
        `Generate one at ${info.console_url} and paste the output here.`,
      status: 400,
    });
  }
  if (!deps.cipher) return render({ byok_error: MASTER_KEY_MISSING, status: 503 });
  repos.byok.store(info.provider, await deps.cipher.encrypt(secret), maskSecret(secret));
  return render({ byok_flash: `Your ${info.label} key is saved. AI features now use your account.` });
}

export function byokDisconnect(repos: UserRepos, req: PageRequest, freeTierConfigured: boolean): PageResult {
  const info = parseProvider(req.params['provider'] ?? '', repos);
  repos.byok.delete(info.provider);
  if (repos.prefs.getActiveByokProvider() === info.provider) repos.prefs.setActiveByokProvider(null);
  return renderAgentSettings(repos, freeTierConfigured, { byok_flash: 'API key removed.' });
}

export function byokUse(repos: UserRepos, req: PageRequest, freeTierConfigured: boolean): PageResult {
  const info = parseProvider(req.params['provider'] ?? '', repos);
  if (info.provider === RETIRED_PROVIDER) {
    return renderAgentSettings(repos, freeTierConfigured, { byok_error: SUBSCRIPTION_RETIRED, status: 400 });
  }
  if (repos.byok.metadata(info.provider) === null) {
    return renderAgentSettings(repos, freeTierConfigured, { byok_error: `Add a ${info.label} key before making it active.`, status: 400 });
  }
  repos.prefs.setActiveByokProvider(info.provider);
  return renderAgentSettings(repos, freeTierConfigured, { byok_flash: `${info.label} is now your active provider.` });
}
