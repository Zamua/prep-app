// What a route hands a progress partial. The ledger keeps the status
// literal beside its progress keys; every partial reads one flat object.
import type { JobStatus } from '../ports.js';

export const flatten = (s: JobStatus): Record<string, unknown> => ({ ...s.progress, status: s.status });

/** No `job_progress` row: the job never started here, or the prune took it.
 * A fresh object each call, because a route may add keys to it. */
export const gone = (): Record<string, unknown> => ({ status: 'gone' });

/** The statuses the grading poll stops on. The upper-case four are only
 * reachable on rows written before the current runner and are kept so an
 * old row still terminates the poll. */
export const TERMINAL_GRADING: readonly string[] = ['done', 'failed', 'COMPLETED', 'FAILED', 'CANCELED', 'TERMINATED'];
