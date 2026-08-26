// The signed-in home page: the deck source the shell embeds, the overflow
// menus rendered from the same summaries, and the two continue strips.
import { formatDone, type DoneItem } from '../../domain/trivia.js';
import type { UserRepos } from '../ports.js';
import { menuContext, overviewPayload } from './overview.js';

export function dashboard(repos: UserRepos): Record<string, unknown> {
  const summaries = repos.decks.listSummaries();
  const activeTrivia = repos.trivia.listActiveSessions().map((s) => ({
    deck_name: s.deck_name,
    deck_display: s.deck_display_name || s.deck_name,
    deck_id: s.deck_id,
    remaining: s.queue.length,
    total: s.queue.length + s.done.length,
    last_active: s.last_active,
    queue_param: s.queue.join(','),
    done_param: formatDone(s.done as DoneItem[]),
  }));
  const snoozed: Record<string, unknown>[] = [
    ...repos.sessions.listSnoozed().map((s) => ({
      kind: 'srs',
      id: s.id,
      deck_name: s.deck_name,
      deck_display: s.deck_display_name || s.deck_name,
      snoozed_until: s.snoozed_until,
    })),
    ...repos.trivia.listSnoozedSessions().map((s) => ({
      kind: 'trivia',
      deck_name: s.deck_name,
      deck_display: s.deck_display_name || s.deck_name,
      snoozed_until: s.snoozed_until,
    })),
  ];
  // Soonest wakes first, same order on both sides of the merge.
  snoozed.sort((a, b) => {
    const x = String(a['snoozed_until'] ?? '');
    const y = String(b['snoozed_until'] ?? '');
    return x < y ? -1 : x > y ? 1 : 0;
  });
  return {
    dashboard_overview: overviewPayload(repos, repos.prefs.get()),
    ...menuContext(summaries),
    recent_sessions: repos.sessions.listRecent(5),
    active_trivia_sessions: activeTrivia,
    snoozed_sessions: snoozed,
  };
}
