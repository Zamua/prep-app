// The one direction status travels: a JobCell writes into its owner's cell,
// once per transition. Transcribed from prep/workflows/service.py, so the
// tests pin the notification rules by the names the rows carry.
import { beforeEach, describe, expect, it } from 'vitest';
import { applyJobStatus, deliverJobStatus } from '../../app/jobs/status.js';
import type { JobStatusWrite, PushOutcome, UserRepos } from '../../app/ports.js';
import type { PushSubscription } from '../../app/entities.js';
import { cell, USER, type Cell } from '../repos/setup.js';

const JOB = 'plan-capitals-abcdef0123';

const write = (over: Partial<JobStatusWrite> = {}): JobStatusWrite => ({
  jobId: JOB,
  transition: 1,
  status: 'planning',
  progress: { status: 'planning', started_at: '2026-03-14T15:00:00+00:00' },
  urlPath: `/plan/${JOB}`,
  kind: 'plan',
  deckId: 1,
  deckName: 'capitals',
  ...over,
});

let c: Cell;
let repos: UserRepos;
const sent: string[] = [];
const webPush = {
  async send(_sub: PushSubscription, payload: string): Promise<PushOutcome> {
    sent.push(payload);
    return 'ok';
  },
};
const deps = () => ({ repos, webPush, vapidPublicKey: '' });

beforeEach(() => {
  c = cell();
  repos = c.repos;
  repos.pushSubs.upsert('https://push.example/1', 'p256dh', 'auth');
  sent.length = 0;
});

describe('one transition', () => {
  it('registers the badge row and the progress row together', () => {
    repos.tx.sync(() => applyJobStatus(repos, write()));
    expect(repos.jobs.get(JOB)).toMatchObject({ workflow_type: 'plan', status: 'planning', url_path: `/plan/${JOB}`, deck_name: 'capitals' });
    expect(repos.jobProgress.get(JOB)).toEqual({ status: 'planning', progress: { status: 'planning', started_at: '2026-03-14T15:00:00+00:00' } });
    expect(repos.jobProgress.transitionOf(JOB)).toBe(1);
  });

  it('drops a re-delivered transition before any side effect', () => {
    repos.tx.sync(() => applyJobStatus(repos, write()));
    repos.tx.sync(() => applyJobStatus(repos, write({ transition: 2, status: 'awaiting_feedback' })));
    const again = repos.tx.sync(() => applyJobStatus(repos, write({ transition: 2, status: 'awaiting_feedback' })));
    expect(again).toEqual({ applied: false, push: null });
    expect(repos.jobProgress.transitionOf(JOB)).toBe(2);
  });

  it('drops an out-of-order transition rather than walking the row backwards', () => {
    repos.tx.sync(() => applyJobStatus(repos, write({ transition: 5, status: 'generating' })));
    repos.tx.sync(() => applyJobStatus(repos, write({ transition: 3, status: 'planning' })));
    expect(repos.jobProgress.get(JOB)?.status).toBe('generating');
    expect(repos.jobs.get(JOB)?.status).toBe('generating');
  });

  it('carries the whole progress payload, which is what the partial renders', () => {
    const progress = { status: 'generating', plan: [{ title: 'a' }], round: 2, total: 3, generated_count: 1 };
    repos.tx.sync(() => applyJobStatus(repos, write({ transition: 4, status: 'generating', progress })));
    expect(repos.jobProgress.get(JOB)?.progress).toEqual(progress);
  });

  it('renders nothing for a workflow with no row: the `gone` case', () => {
    expect(repos.jobProgress.get('plan-missing-0000000000')).toBeNull();
  });
});

describe('the notification rules', () => {
  const drive = async (steps: [number, string][]) => {
    for (const [transition, status] of steps) await deliverJobStatus(deps(), write({ transition, status }));
  };

  it('pushes once on the first awaiting-action transition', async () => {
    await drive([
      [1, 'planning'],
      [2, 'awaiting_feedback'],
      [3, 'awaiting_feedback'],
    ]);
    expect(sent.length).toBe(1);
    expect(JSON.parse(sent[0]!)).toMatchObject({ title: 'Prep — action required', body: 'Plan for capitals is ready to review.', url: `/plan/${JOB}` });
    expect(repos.jobs.get(JOB)?.notified_action_at).not.toBeNull();
  });

  it('pushes on an unattended terminal', async () => {
    await drive([
      [1, 'grading'],
      [2, 'done'],
    ]);
    expect(sent.length).toBe(1);
    expect(JSON.parse(sent[0]!)).toMatchObject({ title: 'Prep — done', body: 'Plan for capitals is done.' });
    expect(repos.jobs.get(JOB)?.terminal_at).not.toBeNull();
  });

  it('stays silent on a terminal the user chose, because the action push already asked', async () => {
    await drive([
      [1, 'planning'],
      [2, 'awaiting_feedback'],
      [3, 'accepting'],
      [4, 'done'],
    ]);
    expect(sent.length).toBe(1);
    expect(JSON.parse(sent[0]!)['title']).toBe('Prep — action required');
    expect(repos.jobs.get(JOB)?.notified_terminal_at).toBeNull();
    expect(repos.jobs.get(JOB)?.terminal_at).not.toBeNull();
  });

  it('names the failure by workflow type', async () => {
    await drive([
      [1, 'computing'],
      [2, 'failed'],
    ]);
    expect(JSON.parse(sent[0]!)['body']).toBe('plan on capitals failed.');
  });

  it('stamps terminal_at once', async () => {
    await drive([
      [1, 'grading'],
      [2, 'done'],
    ]);
    const first = repos.jobs.get(JOB)!.terminal_at;
    c.clock.advance(60_000);
    await drive([[3, 'failed']]);
    expect(repos.jobs.get(JOB)?.terminal_at).toBe(first);
  });

  it('fires no second push when the same transition is delivered twice', async () => {
    await drive([[1, 'planning']]);
    await deliverJobStatus(deps(), write({ transition: 2, status: 'awaiting_feedback' }));
    await deliverJobStatus(deps(), write({ transition: 2, status: 'awaiting_feedback' }));
    expect(sent.length).toBe(1);
  });

  it('applies the rules to a row it creates itself, which has no prior status', async () => {
    await drive([[7, 'done']]);
    expect(sent.length).toBe(1);
    expect(repos.jobs.get(JOB)?.terminal_at).not.toBeNull();
  });

  it('logs the notification for the notify page', async () => {
    await drive([
      [1, 'grading'],
      [2, 'done'],
    ]);
    expect(repos.notify.listRecent().map((e) => e.source)).toEqual(['workflow']);
  });
});

describe('the trivia and grading wording', () => {
  it('uses the type-specific terminal body', async () => {
    await deliverJobStatus(deps(), write({ jobId: 'trivia-x-1', transition: 1, status: 'generating', kind: 'trivia_gen' }));
    await deliverJobStatus(deps(), write({ jobId: 'trivia-x-1', transition: 2, status: 'done', kind: 'trivia_gen' }));
    expect(JSON.parse(sent.at(-1)!)['body']).toBe('Trivia for capitals is ready.');

    sent.length = 0;
    await deliverJobStatus(deps(), write({ jobId: 'grade-x-q1-1', transition: 1, status: 'grading', kind: 'grading' }));
    await deliverJobStatus(deps(), write({ jobId: 'grade-x-q1-1', transition: 2, status: 'done', kind: 'grading' }));
    expect(JSON.parse(sent.at(-1)!)['body']).toBe('Grading is done — capitals.');
  });
});

describe('pruning', () => {
  it('drops a progress row whose workflow is gone', () => {
    repos.tx.sync(() => applyJobStatus(repos, write({ transition: 1, status: 'planning' })));
    repos.tx.sync(() => applyJobStatus(repos, write({ transition: 2, status: 'done' })));
    c.clock.advance(25 * 3_600_000);
    expect(repos.jobs.pruneTerminalOlderThan()).toBe(1);
    expect(repos.jobProgress.pruneOrphans()).toBe(1);
    expect(repos.jobProgress.get(JOB)).toBeNull();
  });
});

describe('the owner', () => {
  it('is the cell, not a column: nothing here names a user', () => {
    repos.tx.sync(() => applyJobStatus(repos, write()));
    const cols = [...c.storage.db.prepare('SELECT name FROM pragma_table_info(?)').all('job_progress')].map((r) => (r as { name: string }).name);
    expect(cols).toEqual(['workflow_id', 'payload', 'transition', 'updated_at']);
    expect(USER).toBeTruthy();
  });
});
