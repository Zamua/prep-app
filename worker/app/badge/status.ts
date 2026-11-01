// The three status buckets the badge sorts and labels by, from the one
// classification every job surface reads.
import { jobPhase } from '../../domain/jobs/graph.js';

export const isActionRequired = (status: string): boolean => jobPhase(status) === 'awaiting';
export const isTerminal = (status: string): boolean => jobPhase(status) === 'terminal';
export const isInProgress = (status: string): boolean => jobPhase(status) === 'running';

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
