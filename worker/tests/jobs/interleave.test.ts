// RPCs that land on a JobCell while it awaits a step: the runtime runs them
// in the gap, so what the cell commits afterwards has to answer to them.
import { beforeEach, describe, expect, it } from 'vitest';
import { llmStep, writeStep } from '../../app/jobs/registry.js';
import type { StepGraph } from '../../domain/jobs/graph.js';
import { jobHarness, seedOwner, type JobHarness } from './harness.js';
import { USER } from '../repos/setup.js';

const HOUR = 3_600_000;
const ONCE = { attempts: 1, initialMs: 1_000, coefficient: 2, capMs: 30_000 };

/** plan -> gate(1h, reject on deadline, feedback re-plans) -> apply. */
const GRAPH: StepGraph = {
  kind: 'Demo',
  partial: null,
  doneStatus: 'done',
  nodes: [
    { name: 'plan', kind: 'llm', retry: ONCE, status: 'planning' },
    {
      name: 'gate',
      kind: 'gate',
      retry: ONCE,
      status: 'awaiting_feedback',
      gate: {
        events: ['accept', 'reject', 'feedback'],
        deadlineMs: HOUR,
        refreshOnEvent: false,
        onEvent: {
          accept: { transient: 'accepting', go: 'proceed' },
          reject: { transient: 'rejecting', go: 'reject' },
          feedback: { transient: 'replanning', go: { rerun: 'plan' } },
        },
        onDeadline: 'reject',
        rerunError: 'replan failed: ',
      },
    },
    { name: 'apply', kind: 'write', retry: ONCE, status: 'applying' },
  ],
};

const ID = 'plan-demo-0123456789';

/** A call held in flight: `entered` settles once it is, `release` lets it finish. */
function hold(): { entered: Promise<void>; release: () => void; wait: () => Promise<void> } {
  let enter!: () => void;
  let release!: () => void;
  const entered = new Promise<void>((r) => (enter = r));
  const released = new Promise<void>((r) => (release = r));
  return {
    entered,
    release,
    wait: () => {
      enter();
      return released;
    },
  };
}

function register(h: JobHarness, planRound: (item: number) => Promise<void> = async () => {}): { apply: string[] } {
  const calls = { apply: [] as string[] };
  h.registry.register(
    'plan',
    llmStep(async (ctx) => {
      await planRound(ctx.item);
      return { value: ['a'], items: ['a'], progress: {} };
    }),
  );
  h.registry.register(
    'apply',
    writeStep(async (ctx) => {
      calls.apply.push(ctx.stepKey);
      return { value: 1 };
    }),
  );
  return calls;
}

const at = (h: JobHarness): string => h.clock.now().toISOString();

async function start(h: JobHarness): Promise<void> {
  await h.jobCell(ID).start({
    id: ID,
    kind: 'Demo',
    owner: USER,
    input: { deckName: 'demo' },
    urlPath: `/plan/${ID}`,
    workflowType: 'plan',
    deckId: 1,
    deckName: 'demo',
    at: at(h),
  });
}

const statuses = (h: JobHarness): unknown[] => h.ledger(ID).outbox.map((o) => o['status']);

let h: JobHarness;
beforeEach(() => {
  h = jobHarness({ graphs: { Demo: GRAPH } });
  seedOwner(h, USER, { push: false });
});

describe('a step in flight', () => {
  it('stays terminated when terminate lands mid-step', async () => {
    const step = hold();
    const calls = register(h, () => step.wait());
    await start(h);
    const tick = h.tick(ID);
    await step.entered;
    await h.jobCell(ID).terminate('cancelled', at(h));
    step.release();
    await tick;
    await h.settleThrough(2 * HOUR);

    const l = h.ledger(ID);
    expect([l.job['state'], l.job['terminal_status'], l.job['error']]).toEqual(['terminal', 'failed', 'cancelled']);
    expect(statuses(h)).toEqual(['planning', 'failed']);
    expect(calls.apply).toEqual([]);
  });

  it('stays terminated when terminate lands mid-step and the step then fails', async () => {
    const step = hold();
    register(h, async (item) => {
      if (item === 0) return;
      await step.wait();
      throw new Error('model unavailable');
    });
    await start(h);
    await h.settle();
    await h.jobCell(ID).signal({ name: 'feedback', at: at(h) });
    const tick = h.tick(ID);
    await step.entered;
    await h.jobCell(ID).terminate('cancelled', at(h));
    step.release();
    await tick;

    const l = h.ledger(ID);
    expect([l.job['state'], l.job['terminal_status'], l.job['error']]).toEqual(['terminal', 'failed', 'cancelled']);
    expect(statuses(h)).toEqual(['planning', 'awaiting_feedback', 'replanning', 'failed']);
  });
});
