// The three status buckets the badge sorts and labels by. Status strings are
// workflow-type specific; these sets are the single source of truth for the
// UI. The upper-case four are only on rows written before the current
// runner, and are kept so an old badge still reads as finished.

export const ACTION_REQUIRED_STATUSES: readonly string[] = ['awaiting_apply', 'awaiting_feedback'];

export const TERMINAL_STATUSES: readonly string[] = ['done', 'failed', 'rejected', 'gone', 'COMPLETED', 'FAILED', 'CANCELED', 'TERMINATED'];

export const isActionRequired = (status: string): boolean => ACTION_REQUIRED_STATUSES.includes(status);
export const isTerminal = (status: string): boolean => TERMINAL_STATUSES.includes(status);
/** Anything not terminal and not awaiting-action, including transient values. */
export const isInProgress = (status: string): boolean => !isTerminal(status) && !isActionRequired(status);

/** Short label for the popover row; the popover is narrow. */
export function displayStatus(status: string): string {
  if (status === 'awaiting_apply') return 'review';
  if (status === 'awaiting_feedback') return 'review plan';
  if (status === 'done' || status === 'COMPLETED') return 'done';
  if (status === 'failed' || status === 'FAILED') return 'failed';
  if (status === 'rejected' || status === 'CANCELED' || status === 'TERMINATED' || status === 'gone') return 'cancelled';
  if (status === 'asking_ai') return 'asking AI';
  return status || 'starting';
}

export function displayLabel(w: { deck_display_name: string | null; deck_name: string | null; workflow_type: string }): string {
  if (w.deck_display_name) return w.deck_display_name;
  if (w.deck_name) return w.deck_name;
  if (w.workflow_type === 'transform') return 'reorganize';
  return w.workflow_type.split('_').join(' ');
}
