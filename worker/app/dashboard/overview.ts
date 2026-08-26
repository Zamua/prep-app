// The dashboard's JSON and menu surfaces, transcribed from
// prep/web/dashboard.py. Both read the deck summaries the HTML route
// already reads, so a menu can never describe a deck the list omits.
import type { DeckSummary, Profile } from '../entities.js';
import { json, type ApiResult } from '../http.js';
import type { UserRepos } from '../ports.js';

/**
 * The DeckSource overview. The raw account id is deliberately absent: an
 * anonymous account's id must never reach a rendered page. `unsynced` is
 * null because an outbox is a client-side store, so the server has
 * nothing to answer with.
 */
export function overviewPayload(repos: UserRepos, user: Profile | null): Record<string, unknown> {
  const summaries = repos.decks.listSummaries();
  const decks = summaries.map((d) => ({
    id: d.id,
    slug: d.name,
    display_name: d.display_name || d.name,
    due: d.due,
    total: d.total,
    deck_type: d.deck_type,
    pinned: d.pinned,
    trivia_stats: d.deck_type === 'trivia' ? repos.trivia.deckStats(d.id) : null,
  }));
  return {
    user: { display_name: user?.display_name || null, is_anonymous: Boolean(user?.is_anonymous) },
    decks,
    due: decks.reduce((n, d) => n + d.due, 0),
    total: decks.reduce((n, d) => n + d.total, 0),
    nextDueMinutes: repos.cards.nextDueMinutes(),
    unsynced: null,
  };
}

export function overview(repos: UserRepos, user: Profile | null): ApiResult {
  return json(overviewPayload(repos, user));
}

/** The rows read the same summary dicts the deck list is built from. */
export function menuContext(summaries: DeckSummary[]): Record<string, unknown> {
  return { menu_decks: summaries };
}

export function deckMenus(repos: UserRepos): ApiResult {
  return { page: 'partials/deck_menus.html', context: menuContext(repos.decks.listSummaries()) };
}
