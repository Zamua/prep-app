// Which credential funds one owner's call, and whether any does. Transcribed
// from prep/agent/selector.py: `agent_for_user`, `funding_tier_for_user` and
// `require_funded_workflow`, minus the retired subscription branch.
//
// Policy over rows, so it is app-layer: no adapter is named here, and the
// answer is a value the composition root turns into an adapter.
import { AgentUnavailable, type AgentConfig, type FundingTier, type UserRepos } from '../ports.js';

/** Python's `_NO_FUNDING`, raised by a start no tier would fund. */
export const NO_FUNDING = 'AI is not configured. Add a personal API key on /settings/agent, or ask the deploy admin to configure a shared tier.';

/** Python's `_ANON_NO_AGENT`. A guest's one AI path is the instant endpoint,
 * which resolves the free tier directly and never comes through here. */
export const ANON_NO_AGENT = 'AI is not configured for a guest account. Create an account and add a personal API key on /settings/agent to generate cards.';

/** Rows exist but none of them yielded a key. Falling through to the shared
 * tier would serve a credential this owner opted out of. */
export const BYOK_UNUSABLE = 'AI is unavailable: your saved API key could not be used. Re-add it on /settings/agent, or ask the deploy admin to check the server logs.';

/** Per-generation card ceiling for a call the shared tier funds; a start
 * passes it as the job's max-cards / batch size. BYOK is uncapped. */
export const FREE_TIER_MAX_CARDS_PER_CALL = 5;

/** Precedence when an owner holds several keys and named no active one.
 * Anthropic leads: the prompts were written against that model surface. */
const BYOK_ORDER = ['anthropic-api', 'openrouter-api', 'openai-api'] as const;

function hasByokRows(repos: UserRepos): boolean {
  try {
    return repos.byok.listProviders().length > 0;
  } catch {
    // Unknown state fails closed: this owner may hold a key, and a shared
    // credential must never quietly stand in for one.
    return true;
  }
}

function isAnonymous(repos: UserRepos): boolean {
  try {
    return (repos.prefs.get()?.is_anonymous ?? 0) !== 0;
  } catch {
    // An unreadable profile breaks the request either way; denying AI to
    // every owner would turn it into a deploy-wide outage.
    return false;
  }
}

/** Which tier WOULD fund this owner, from row existence alone. A failed row
 * check answers `byok` for the same reason the selector refuses. */
export function fundingTier(repos: UserRepos, freeTierConfigured: boolean): FundingTier {
  if (isAnonymous(repos)) return 'none';
  if (hasByokRows(repos)) return 'byok';
  return freeTierConfigured ? 'free' : 'none';
}

/** The credential itself. Only "no row for this owner" continues to the
 * shared tier. */
export function agentConfig(repos: UserRepos, freeTierConfigured: boolean): AgentConfig {
  if (isAnonymous(repos)) return { tier: 'none', reason: ANON_NO_AGENT };
  if (hasByokRows(repos)) {
    try {
      const order: string[] = [];
      const chosen = repos.prefs.getActiveByokProvider();
      if (chosen && (BYOK_ORDER as readonly string[]).includes(chosen)) order.push(chosen);
      for (const p of BYOK_ORDER) if (!order.includes(p)) order.push(p);
      for (const provider of order) {
        const ciphertext = repos.byok.getCiphertext(provider);
        if (ciphertext) return { tier: 'byok', provider, ciphertext };
      }
    } catch {
      return { tier: 'none', reason: BYOK_UNUSABLE };
    }
    return { tier: 'none', reason: BYOK_UNUSABLE };
  }
  return freeTierConfigured ? { tier: 'free' } : { tier: 'none', reason: NO_FUNDING };
}

/** Refuse a start no tier would fund. A start registers a badge row and holds
 * a job cell, so the refusal belongs before it, not inside the first step. */
export function requireFundedWorkflow(repos: UserRepos, freeTierConfigured: boolean): void {
  if (fundingTier(repos, freeTierConfigured) === 'none') throw new AgentUnavailable(NO_FUNDING);
}
