// The deploy's shared free tier: one OpenAI-compatible endpoint, funded by a
// credential every user shares. Configured only when base URL, key and model
// are all set, so a half-configured deploy has no free tier rather than a
// broken one.
//
// `shared: true` is what makes a 429 here contention rather than a user's
// own budget, which is a different message to the user.
import type { CompatConfig } from './openaiCompat.js';
import { OpenAICompatAgent } from './openaiCompat.js';

export interface FreeTierEnv {
  PREP_FREE_INFERENCE_BASE_URL?: string;
  PREP_FREE_INFERENCE_API_KEY?: string;
  PREP_FREE_INFERENCE_MODEL?: string;
  PREP_FREE_INFERENCE_EXTRA_BODY?: string;
}

export type FreeTierConfig = CompatConfig;

export const FREE_TIER_LABEL = 'free AI';

/** The instant endpoint's own output cap: its abuse arithmetic assumes it. */
export const INSTANT_MAX_OUTPUT_TOKENS = 1024;
/** A deck-wide transform's output scales with the deck, so the job cap is the
 * endpoint's context, not the compat default. */
export const FREE_TIER_MAX_OUTPUT_TOKENS = 32768;
export const DEFAULT_TIMEOUT_MS = 60_000;

export interface FreeTierOpts {
  maxTokens?: number;
  timeoutMs?: number;
  warn?: (msg: string) => void;
}

/** Never throws: this answers on every page render, so an escaping error
 * would be a deploy-wide 500 rather than a degraded feature. */
export function freeTierConfig(env: FreeTierEnv, opts: FreeTierOpts = {}): FreeTierConfig | null {
  const warn = opts.warn ?? console.error;
  const present: Record<string, string> = {
    PREP_FREE_INFERENCE_BASE_URL: (env.PREP_FREE_INFERENCE_BASE_URL ?? '').trim(),
    PREP_FREE_INFERENCE_API_KEY: (env.PREP_FREE_INFERENCE_API_KEY ?? '').trim(),
    PREP_FREE_INFERENCE_MODEL: (env.PREP_FREE_INFERENCE_MODEL ?? '').trim(),
  };
  const set = Object.entries(present).filter(([, v]) => v);
  if (set.length === 0) return null; // feature off, the normal silent case
  const missing = Object.entries(present).filter(([, v]) => !v);
  if (missing.length > 0) {
    warn(`free tier disabled: half-configured - ${set.map(([n]) => n).join(', ')} set but ${missing.map(([n]) => n).join(', ')} missing`);
    return null;
  }

  // Parsed once here, never per request: a body we do not understand
  // disables the tier instead of shipping.
  let extraBody: Record<string, unknown> | null = null;
  const raw = (env.PREP_FREE_INFERENCE_EXTRA_BODY ?? '').trim();
  if (raw) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      warn('free tier disabled: PREP_FREE_INFERENCE_EXTRA_BODY is not valid JSON');
      return null;
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      warn('free tier disabled: PREP_FREE_INFERENCE_EXTRA_BODY must be a JSON object');
      return null;
    }
    extraBody = parsed as Record<string, unknown>;
  }

  return {
    apiKey: present['PREP_FREE_INFERENCE_API_KEY']!,
    baseUrl: present['PREP_FREE_INFERENCE_BASE_URL']!.replace(/\/+$/, ''),
    model: present['PREP_FREE_INFERENCE_MODEL']!,
    extraBody,
    shared: true,
    maxTokens: opts.maxTokens ?? FREE_TIER_MAX_OUTPUT_TOKENS,
    timeoutMs: opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    label: FREE_TIER_LABEL,
  };
}

export class FreeTierAgent extends OpenAICompatAgent {}
