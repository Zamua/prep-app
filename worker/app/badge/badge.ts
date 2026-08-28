// The masthead badge fragment. The stale-terminal cleanup rides the read
// path: one indexed DELETE per poll is cheaper than a scheduler.
import type { ActiveWorkflow } from '../entities.js';
import type { ApiResult } from '../http.js';
import type { UserRepos } from '../ports.js';
import { displayLabel, displayStatus, isActionRequired, isTerminal } from './status.js';

/** The template reads the bucket flags off each row, so they travel with it. */
export function workflowRow(w: ActiveWorkflow): Record<string, unknown> {
  return {
    ...w,
    is_action_required: isActionRequired(w.status),
    is_terminal: isTerminal(w.status),
    is_in_progress: !isTerminal(w.status) && !isActionRequired(w.status),
    display_status: displayStatus(w.status),
    display_label: displayLabel(w),
  };
}

/** Awaiting-action first, then in-progress, then just-completed; the SQL
 * already ordered newest-first inside each group. */
const bucket = (w: ActiveWorkflow): number => (isActionRequired(w.status) ? 0 : isTerminal(w.status) ? 2 : 1);

export function workflowBadge(repos: UserRepos): ApiResult {
  repos.jobs.cleanupStaleTerminal();
  const rows = repos.jobs.listForUser();
  const sorted = rows.map((w, i) => ({ w, i })).sort((a, b) => bucket(a.w) - bucket(b.w) || a.i - b.i);
  return { page: 'partials/workflow_badge.html', context: { workflows: sorted.map((e) => workflowRow(e.w)) } };
}
