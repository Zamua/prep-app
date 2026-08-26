// The step ledger over one JobCell's storage. Every write of an activation
// lands through `commit`, in one `transactionSync`: the step row, the cursor,
// the next node's rows and the outbox row are one fact, not four.
import type { JobLedger } from '../../../app/ports.js';
import type { EventRow, JobRow, LedgerCommit, LedgerRows, OutboxRow, StepRow } from '../../../domain/jobs/ledger.js';
import { Db, type CellStorage, type Row } from './storage.js';

const text = (v: unknown): string | null => (v == null ? null : String(v));
const json = (v: unknown): unknown => (v == null ? null : JSON.parse(String(v)));

function toJob(r: Row): JobRow {
  return {
    id: String(r['id']),
    kind: String(r['kind']),
    owner: String(r['owner']),
    input: (json(r['input']) ?? {}) as Record<string, unknown>,
    state: String(r['state']) as JobRow['state'],
    cursor: Number(r['cursor']),
    created_at: String(r['created_at']),
    deadline_at: text(r['deadline_at']),
    deadline_kind: text(r['deadline_kind']),
    terminal_at: text(r['terminal_at']),
    terminal_status: text(r['terminal_status']),
    error: text(r['error']),
    transition: Number(r['transition']),
  };
}

function toStep(r: Row): StepRow {
  return {
    step_key: String(r['step_key']),
    name: String(r['name']),
    idx: Number(r['idx']),
    item: Number(r['item']),
    status: String(r['status']) as StepRow['status'],
    attempt: Number(r['attempt']),
    refusals: Number(r['refusals']),
    next_attempt_at: text(r['next_attempt_at']),
    output: json(r['output']),
    error: text(r['error']),
    started_at: text(r['started_at']),
    finished_at: text(r['finished_at']),
  };
}

const toEvent = (r: Row): EventRow => ({
  seq: Number(r['seq']),
  name: String(r['name']),
  payload: json(r['payload']),
  at: String(r['at']),
  consumed_at: text(r['consumed_at']),
});

const toOutbox = (r: Row): OutboxRow => ({
  transition: Number(r['transition']),
  status: String(r['status']),
  payload: (json(r['payload']) ?? {}) as Record<string, unknown>,
  at: String(r['at']),
  delivered_at: text(r['delivered_at']),
  attempt: Number(r['attempt']),
  next_attempt_at: text(r['next_attempt_at']),
});

export class SqlJobLedger implements JobLedger {
  private readonly db: Db;

  constructor(private readonly storage: CellStorage) {
    this.db = new Db(storage.sql);
  }

  read(): LedgerRows | null {
    const row = this.db.first('SELECT * FROM job LIMIT 1');
    if (!row) return null;
    return {
      job: toJob(row),
      steps: this.db.all('SELECT * FROM steps ORDER BY idx, item').map(toStep),
      events: this.db.all('SELECT * FROM events ORDER BY seq').map(toEvent),
      outbox: this.db.all('SELECT * FROM outbox ORDER BY transition').map(toOutbox),
    };
  }

  create(job: {
    id: string;
    kind: string;
    owner: string;
    input: Record<string, unknown>;
    createdAt: string;
    urlPath: string;
    workflowType: string;
    deckId: number | null;
    deckName: string | null;
  }): boolean {
    return (
      this.db.run(
        `INSERT OR IGNORE INTO job (id, kind, owner, input, state, cursor, created_at, transition, url_path, workflow_type, deck_id, deck_name)
         VALUES (?, ?, ?, ?, 'running', 0, ?, 0, ?, ?, ?, ?)`,
        job.id,
        job.kind,
        job.owner,
        JSON.stringify(job.input),
        job.createdAt,
        job.urlPath,
        job.workflowType,
        job.deckId,
        job.deckName,
      ) > 0
    );
  }

  route(): { urlPath: string; workflowType: string; deckId: number | null; deckName: string | null } {
    const row = this.db.first('SELECT url_path, workflow_type, deck_id, deck_name FROM job LIMIT 1');
    return {
      urlPath: row ? String(row['url_path']) : '',
      workflowType: row ? String(row['workflow_type']) : '',
      deckId: row && row['deck_id'] != null ? Number(row['deck_id']) : null,
      deckName: row ? text(row['deck_name']) : null,
    };
  }

  appendEvent(event: { name: string; payload: unknown; at: string }): void {
    this.db.run('INSERT INTO events (name, payload, at) VALUES (?, ?, ?)', event.name, event.payload === undefined ? null : JSON.stringify(event.payload), event.at);
  }

  commit(commit: LedgerCommit): void {
    this.storage.transactionSync(() => {
      const step = commit.step;
      if (step) {
        this.db.run(
          `UPDATE steps SET status = ?, attempt = ?, refusals = ?, next_attempt_at = ?, output = ?, error = ?, started_at = ?, finished_at = ?
            WHERE step_key = ?`,
          step.status,
          step.attempt,
          step.refusals,
          step.next_attempt_at,
          step.output === undefined || step.output === null ? null : JSON.stringify(step.output),
          step.error,
          step.started_at,
          step.finished_at,
          step.step_key,
        );
      }
      for (const row of commit.materialize ?? []) {
        this.db.run(
          `INSERT OR IGNORE INTO steps (step_key, name, idx, item, status, attempt, refusals) VALUES (?, ?, ?, ?, 'pending', 0, 0)`,
          row.step_key,
          row.name,
          row.idx,
          row.item,
        );
      }
      const consume = commit.consumeEvents;
      if (consume && consume.throughSeq === null) {
        this.db.run('UPDATE events SET consumed_at = ? WHERE consumed_at IS NULL', consume.at);
      } else if (consume) {
        this.db.run('UPDATE events SET consumed_at = ? WHERE consumed_at IS NULL AND (seq <= ? OR name = ?)', consume.at, consume.throughSeq, consume.name ?? '');
      }
      const job = commit.job;
      if (job && Object.keys(job).length) {
        const cols = Object.keys(job);
        const set = cols.map((c) => `${c} = ?`).join(', ');
        this.db.run(`UPDATE job SET ${set}`, ...cols.map((c) => (job as Record<string, unknown>)[c]));
      }
      const outbox = commit.outbox;
      if (outbox) {
        this.db.run(
          `INSERT OR IGNORE INTO outbox (transition, status, payload, at, attempt, next_attempt_at) VALUES (?, ?, ?, ?, 0, ?)`,
          outbox.transition,
          outbox.status,
          JSON.stringify(outbox.payload),
          outbox.at,
          outbox.at,
        );
      }
    });
  }

  markDelivered(transition: number, at: string): void {
    this.db.run('UPDATE outbox SET delivered_at = ? WHERE transition = ? AND delivered_at IS NULL', at, transition);
  }

  deferDelivery(transition: number, attempt: number, nextAt: string): void {
    this.db.run('UPDATE outbox SET attempt = ?, next_attempt_at = ? WHERE transition = ?', attempt, nextAt, transition);
  }
}
