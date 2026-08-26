// The nine context-processor values for a page rendered inside a cell,
// from the cell's own rows. Lane C's app/pageContext.ts replaces this.
import type { UserRepos } from '../../app/ports.js';

export function pageContext(repos: UserRepos, opts: { buildToken: string; appBase: string; authProvider: string }): Record<string, unknown> {
  const user = repos.prefs.get();
  const anonymous = Boolean(user?.is_anonymous);
  const deckDisplay: Record<string, string> = {};
  for (const d of repos.decks.listSummaries()) deckDisplay[d.name] = d.display_name ?? d.name;
  return {
    app_base: opts.appBase,
    user,
    agent_available: !anonymous && repos.byok.listProviders().length > 0,
    auth_provider: opts.authProvider,
    sign_in_url: '',
    sign_up_url: '',
    sign_out_url: '',
    clerk_publishable_key: null,
    clerk_frontend_api_host: null,
    notif_unseen_count: repos.notify.countUnseen(),
    deck_display: deckDisplay,
    static_css_mtime: opts.buildToken,
  };
}
