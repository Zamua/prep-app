// "Sign in with OpenRouter": the one BYOK provider that needs no
// copy-paste. OAuth PKCE with no app registration; the key it mints
// belongs to the user's own OpenRouter account and is stored exactly like
// a pasted one.
import type { Cipher, UserRepos } from '../ports.js';
import { type PageRequest, type PageResult } from '../pageResult.js';
import { maskSecret, renderAgentSettings } from './agent.js';
import { providerInfo } from './providers.js';

export const PKCE_COOKIE = 'prep_or_pkce';
export const AUTHORIZE_URL = 'https://openrouter.ai/auth';
export const CALLBACK_PATH = '/settings/agent/openrouter/callback';
const PKCE_MAX_AGE = 600;

/** The crypto and the exchange, both adapter-owned. */
export interface OpenRouterAuth {
  /** A fresh verifier with its S256 challenge. */
  startChallenge(): Promise<{ verifier: string; challenge: string }>;
  /** The user's new API key, or a rejection whose message the page shows. */
  exchange(code: string, verifier: string): Promise<string>;
}

export interface OpenRouterDeps {
  freeTierConfigured: boolean;
  cipher: Cipher | null;
  auth: OpenRouterAuth;
  appBase: string;
}

const cookie = (value: string, maxAge: number, secure: boolean): string =>
  `${PKCE_COOKIE}=${value}; HttpOnly; Max-Age=${maxAge}; Path=${CALLBACK_PATH}; SameSite=lax${secure ? '; Secure' : ''}`;

export async function openrouterStart(deps: OpenRouterDeps): Promise<PageResult> {
  const { verifier, challenge } = await deps.auth.startChallenge();
  const url = new URL(AUTHORIZE_URL);
  url.searchParams.set('callback_url', `${deps.appBase}${CALLBACK_PATH}`);
  url.searchParams.set('code_challenge', challenge);
  url.searchParams.set('code_challenge_method', 'S256');
  return {
    redirect: url.toString(),
    status: 303,
    headers: { 'set-cookie': cookie(verifier, PKCE_MAX_AGE, deps.appBase.startsWith('https:')) },
  };
}

export async function openrouterCallback(repos: UserRepos, req: PageRequest, deps: OpenRouterDeps): Promise<PageResult> {
  const drop = { 'set-cookie': cookie('', 0, deps.appBase.startsWith('https:')) };
  const refuse = (message: string, status: number): PageResult => ({
    ...renderAgentSettings(repos, deps.freeTierConfigured, { byok_error: message, status }),
    headers: drop,
  });

  const code = req.query.get('code') ?? '';
  const verifier = req.cookies[PKCE_COOKIE] ?? '';
  if (!code) return refuse('OpenRouter did not return an authorization code. Start the connection again.', 400);
  if (!verifier) return refuse('That OpenRouter sign-in expired. Start the connection again.', 400);
  if (!deps.cipher) return refuse("BYOK isn't available on this deploy — the operator hasn't configured PREP_KEY_ENCRYPTION_SECRET.", 503);

  let key: string;
  try {
    key = await deps.auth.exchange(code, verifier);
  } catch (e) {
    return refuse(`OpenRouter refused the key exchange: ${e instanceof Error ? e.message : String(e)}`, 502);
  }
  const info = providerInfo('openrouter-api')!;
  repos.byok.store(info.provider, await deps.cipher.encrypt(key), maskSecret(key));
  repos.prefs.setActiveByokProvider(info.provider);
  return {
    ...renderAgentSettings(repos, deps.freeTierConfigured, { byok_flash: `Your ${info.label} key is saved. AI features now use your account.` }),
    headers: drop,
  };
}
