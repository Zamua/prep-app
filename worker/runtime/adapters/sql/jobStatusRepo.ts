// `active_workflows`: the job status read model the badge polls.
import type { Clock, JobStatusRepo } from '../../../app/ports.js';
import type { ActiveWorkflow } from '../../../app/entities.js';
import { Db, type CellStorage, type Row } from './storage.js';
import { isoNow, isoUtc, shifted } from './time.js';

export const RECENT_TERMINAL_WINDOW_SECONDS = 60;
export const RECONCILER_PRUNE_WINDOW_SECONDS = 24 * 60 * 60;

const asString = (v: unknown): string | null => (v == null ? null : String(v));

function rowToWorkflow(r: Row): ActiveWorkflow {
  return {
    workflow_id: String(r['workflow_id']),
    workflow_type: String(r['workflow_type']),
    deck_id: r['deck_id'] == null ? null : Number(r['deck_id']),
    deck_name: asString(r['deck_name']),
    deck_display_name: 'deck_display_name' in r ? asString(r['deck_display_name']) : null,
    status: (r['status'] as string | null) || '',
    started_at: String(r['started_at']),
    terminal_at: asString(r['terminal_at']),
    url_path: String(r['url_path']),
    notified_action_at: asString(r['notified_action_at']),
    notified_terminal_at: asString(r['notified_terminal_at']),
  };
}

export class SqlJobStatusRepo implements JobStatusRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  register(job: { workflowId: string; workflowType: string; deckId: number | null; deckName: string | null; urlPath: string; initialStatus?: string }): void {
    this.db.run(
      `INSERT OR IGNORE INTO active_workflows (workflow_id, workflow_type, deck_id, deck_name, status, started_at, url_path)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
      job.workflowId,
      job.workflowType,
      job.deckId,
      job.deckName,
      job.initialStatus ?? 'computing',
      isoNow(this.clock),
      job.urlPath,
    );
  }

  get(workflowId: string): ActiveWorkflow | null {
    const row = this.db.first('SELECT * FROM active_workflows WHERE workflow_id = ?', workflowId);
    return row ? rowToWorkflow(row) : null;
  }

  updateStatus(workflowId: string, status: string): void {
    this.db.run('UPDATE active_workflows SET status = ? WHERE workflow_id = ?', status, workflowId);
  }

  setTerminalAt(workflowId: string, terminalAt: string | null = null): void {
    this.db.run('UPDATE active_workflows SET terminal_at = ? WHERE workflow_id = ? AND terminal_at IS NULL', terminalAt || isoNow(this.clock), workflowId);
  }

  markNotified(workflowId: string, kind: 'action' | 'terminal'): void {
    if (kind !== 'action' && kind !== 'terminal') throw new RangeError(`unknown notification kind: ${JSON.stringify(kind)}`);
    const col = kind === 'action' ? 'notified_action_at' : 'notified_terminal_at';
    this.db.run(`UPDATE active_workflows SET ${col} = ? WHERE workflow_id = ? AND ${col} IS NULL`, isoNow(this.clock), workflowId);
  }

  listForUser(opts: { recentTerminalWindowSeconds?: number } = {}): ActiveWorkflow[] {
    const window = opts.recentTerminalWindowSeconds ?? RECENT_TERMINAL_WINDOW_SECONDS;
    const cutoff = isoUtc(shifted(this.clock.now(), -window * 1000));
    return this.db
      .all(
        `SELECT w.*, d.display_name AS deck_display_name
           FROM active_workflows w LEFT JOIN decks d ON d.name = w.deck_name
          WHERE (w.terminal_at IS NULL OR w.terminal_at >= ?)
          ORDER BY w.started_at DESC`,
        cutoff,
      )
      .map(rowToWorkflow);
  }

  cleanupStaleTerminal(opts: { windowSeconds?: number } = {}): number {
    const window = opts.windowSeconds ?? RECENT_TERMINAL_WINDOW_SECONDS;
    const cutoff = isoUtc(shifted(this.clock.now(), -window * 1000));
    return this.db.run('DELETE FROM active_workflows WHERE terminal_at IS NOT NULL AND terminal_at < ?', cutoff);
  }

  listNonTerminal(): ActiveWorkflow[] {
    return this.db.all('SELECT * FROM active_workflows WHERE terminal_at IS NULL ORDER BY started_at ASC').map(rowToWorkflow);
  }

  pruneTerminalOlderThan(opts: { windowSeconds?: number } = {}): number {
    return this.cleanupStaleTerminal({ windowSeconds: opts.windowSeconds ?? RECONCILER_PRUNE_WINDOW_SECONDS });
  }
}
