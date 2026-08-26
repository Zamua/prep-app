// The ledger against a real celld node, killed with SIGKILL at points the
// fake cannot reach: mid-step, at a step boundary, and while a gate's
// deadline is the only thing left running.
//
// Slow by construction (a build, a deploy and several node restarts), so it
// asks to be run rather than joining the default suite:
//   PREP_CRASH_NODE=1 npx vitest run tests/crash --testTimeout=300000 --hookTimeout=900000
// with the scratch MinIO up. Skipped without the flag, and without celld.
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { CONTROL, killNode, readJob, restartNode, signalJob, sleep, startControl, startJob, startNode, stopNode, until, type ControlServer } from './node.js';

const CELLD = process.env['CELLD_BIN'] ?? join(homedir(), '.local', 'bin', 'celld');
const suite = process.env['PREP_CRASH_NODE'] === '1' && existsSync(CELLD) ? describe : describe.skip;

let control: ControlServer;
/** Unique per run: a cell keeps its rows across restarts, and across runs. */
const stamp = Date.now().toString(36);
const jobId = (name: string): string => `probe-${name}-${stamp}`;
const input = (deck: string): Record<string, unknown> => ({ control: CONTROL, deckName: deck });
/** `CELLD_WAKER_TICK_MS`, the orphan-alarm scan; measured at a minute. */
const WAKER_TICK_MS = Number(process.env['CELLD_WAKER_TICK_MS'] ?? 60_000);

suite('a node that dies', () => {
  beforeAll(async () => {
    control = await startControl();
    startNode();
  }, 600_000);

  afterAll(async () => {
    stopNode();
    await control.close();
  });

  it('finishes a job whose LLM step was in flight, writing each row once', async () => {
    const id = jobId('inflight');
    const key = `${id}-probe-llm-0`;
    const arrived = control.hold(key);
    await startJob(id, 'Probe', input(`inflight-${stamp}`));
    await arrived;

    killNode();
    control.release(key);
    restartNode();

    const { view } = await until(id, ['done', 'failed'], 120);
    expect(view.status).toBe('done');
    // Two items, two write steps, two rows, however many times the node died.
    expect(view.progress['rows']).toBe(2);
    expect(view.progress['inserted']).toBe(2);
    // The step was re-run because its row was never written; that is the
    // retry, not a second effect.
    expect(control.seen.filter((k) => k === key).length).toBeGreaterThanOrEqual(1);
  }, 300_000);

  it('re-runs nothing when the kill lands after the step committed', async () => {
    const id = jobId('boundary');
    const key = `${id}-probe-llm-0`;
    await startJob(id, 'Probe', input(`boundary-${stamp}`));
    // `applying` means the LLM step's row is written and the cursor moved.
    await until(id, ['applying', 'done'], 120);
    const before = control.seen.filter((k) => k === key).length;
    expect(before).toBe(1);

    killNode();
    restartNode();

    const { view } = await until(id, ['done', 'failed'], 120);
    expect(view.status).toBe('done');
    expect(control.seen.filter((k) => k === key).length).toBe(1);
    expect(view.progress['rows']).toBe(2);
  }, 300_000);

  it('resumes a job nobody is watching, one waker tick after the restart', async () => {
    const id = jobId('coldstart');
    const key = `${id}-probe-llm-0`;
    control.reply(key, { items: ['only'] });
    const held = control.hold(key);
    await startJob(id, 'Probe', input(`coldstart-${stamp}`));
    await held;
    killNode();
    control.release(key);
    restartNode();

    // Nothing addresses the cell here. celld's orphan-alarm scan is what
    // brings it back, measured at about a minute after a restart, and the
    // alarm the dead node had armed is still the one that is due.
    await sleep(WAKER_TICK_MS + 15_000);
    const view = await readJob(id);
    expect(view?.status).toBe('done');
    expect(view?.progress['rows']).toBe(1);
  }, 300_000);

  it('fires a gate deadline from a cold cell after a restart', async () => {
    const id = jobId('gate');
    await startJob(id, 'ProbeGate', input(`gate-${stamp}`));
    await until(id, ['awaiting_apply'], 120);

    killNode();
    restartNode();

    // The deadline is persisted, so the restart does not extend it and no
    // request is needed to make it fire. The transient `rejecting` is written
    // and delivered in the same activation as the terminal, so a poll cannot
    // see it; the ledger test pins that ordering.
    const { view } = await until(id, ['rejected', 'done', 'failed'], 120);
    expect(view.status).toBe('rejected');
    expect(view.progress['rows']).toBeUndefined();
  }, 300_000);

  it('carries an applied gate through a restart without running the write twice', async () => {
    const id = jobId('applied');
    const key = `${id}-probe-llm-0`;
    control.reply(key, { items: ['a', 'b', 'c'] });
    await startJob(id, 'ProbeGate', input(`applied-${stamp}`));
    await until(id, ['awaiting_apply'], 120);
    const signalled = await signalJob(id, 'apply');
    expect(signalled?.status).toBe('applying');

    killNode();
    restartNode();

    const { view } = await until(id, ['done', 'failed'], 120);
    expect(view.status).toBe('done');
    expect(view.progress['rows']).toBe(3);
  }, 300_000);

  it('treats a refused step as a refusal and lands it once', async () => {
    const id = jobId('refusal');
    const key = `${id}-probe-llm-0`;
    control.reply(key, { refuse: true });
    await startJob(id, 'Probe', input(`refusal-${stamp}`));
    // Two refusals, then the real answer: the refusals cost no attempt, so a
    // node with one attempt of budget still finishes.
    await sleep(1_000);
    control.reply(key, { items: ['x'] });
    const { view } = await until(id, ['done', 'failed'], 120);
    expect(view.status).toBe('done');
    expect(view.progress['rows']).toBe(1);
    expect(control.seen.filter((k) => k === key).length).toBeGreaterThan(1);
  }, 300_000);
});
