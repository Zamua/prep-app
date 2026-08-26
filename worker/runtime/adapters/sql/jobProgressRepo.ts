// `job_progress`: the rendered progress payload a status write carries, one
// row per workflow. The fragment polls read this and never a JobCell, which
// is what keeps a 300s LLM step from blocking anyone's page.
import type { Clock, JobProgressRepo, JobStatus } from '../../../app/ports.js';
import { Db, type CellStorage } from './storage.js';
import { isoNow } from './time.js';

export class SqlJobProgressRepo implements JobProgressRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  get(workflowId: string): JobStatus | null {
    const row = this.db.first<{ payload: string }>('SELECT payload FROM job_progress WHERE workflow_id = ?', workflowId);
    if (!row) return null;
    const progress = JSON.parse(String(row.payload)) as Record<string, unknown>;
    return { status: String(progress['status'] ?? ''), progress };
  }

  transitionOf(workflowId: string): number | null {
    const row = this.db.first<{ transition: number }>('SELECT transition FROM job_progress WHERE workflow_id = ?', workflowId);
    return row ? Number(row.transition) : null;
  }

  /** The guard is in the statement, so a re-delivery cannot walk the row
   * backwards even if a caller skips the read. */
  upsert(row: { workflowId: string; transition: number; status: string; progress: Record<string, unknown> }): void {
    const payload = JSON.stringify({ ...row.progress, status: row.status });
    this.db.run(
      `INSERT INTO job_progress (workflow_id, payload, transition, updated_at) VALUES (?, ?, ?, ?)
       ON CONFLICT(workflow_id) DO UPDATE SET payload = excluded.payload, transition = excluded.transition, updated_at = excluded.updated_at
       WHERE excluded.transition > job_progress.transition`,
      row.workflowId,
      payload,
      row.transition,
      isoNow(this.clock),
    );
  }

  remove(workflowId: string): boolean {
    return this.db.run('DELETE FROM job_progress WHERE workflow_id = ?', workflowId) > 0;
  }

  pruneOrphans(): number {
    return this.db.run('DELETE FROM job_progress WHERE workflow_id NOT IN (SELECT workflow_id FROM active_workflows)');
  }
}
