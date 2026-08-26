// The context-processor values every page inside a cell renders with,
// from the cell's own rows. Python spreads these from nine processors on
// `templates`; here one call yields them and the route's own context is
// merged over the top.
import type { UserRepos } from './ports.js';

export interface AuthUrls {
  signIn: string;
  signUp: string;
  signOut: string;
  clerkPublishableKey: string | null;
  clerkFrontendApiHost: string | null;
}

export const NO_AUTH_URLS: AuthUrls = {
  signIn: '',
  signUp: '',
  signOut: '',
  clerkPublishableKey: null,
  clerkFrontendApiHost: null,
};

export interface PageEnv {
  buildToken: string;
  appBase: string;
  authProvider: string;
  /** The deploy serves a shared tier: `agent_available` without a BYOK row. */
  freeTierConfigured: boolean;
  urls?: AuthUrls;
}

/** True when `agent_for_user` would hand back a usable adapter: never for
 * an anonymous account, else a stored key or the deploy's free tier. */
export function agentAvailable(repos: UserRepos, freeTierConfigured: boolean): boolean {
  if (repos.prefs.get()?.is_anonymous) return false;
  return freeTierConfigured || repos.byok.listProviders().length > 0;
}

export function pageContext(repos: UserRepos, env: PageEnv): Record<string, unknown> {
  const urls = env.urls ?? NO_AUTH_URLS;
  const deckDisplay: Record<string, string> = {};
  for (const d of repos.decks.listSummaries()) deckDisplay[d.name] = d.display_name ?? d.name;
  return {
    app_base: env.appBase,
    user: repos.prefs.get(),
    agent_available: agentAvailable(repos, env.freeTierConfigured),
    auth_provider: env.authProvider,
    sign_in_url: urls.signIn,
    sign_up_url: urls.signUp,
    sign_out_url: urls.signOut,
    clerk_publishable_key: urls.clerkPublishableKey,
    clerk_frontend_api_host: urls.clerkFrontendApiHost,
    notif_unseen_count: repos.notify.countUnseen(),
    deck_display: deckDisplay,
    static_css_mtime: env.buildToken,
  };
}
