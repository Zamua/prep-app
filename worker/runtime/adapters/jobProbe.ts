// A job whose steps do exactly what a real one's do to the ledger and
// nothing else: one outbound call per LLM step, one idempotent row per write
// step. It exists so the crash matrix can kill a node between any two step
// boundaries before the four real workflows are written, and it is composed
// only under `PREP_TEST_MODE`, which the composition root refuses outside
// dev and staging.
//
// The control server the LLM step calls decides what happens: how many items
// the job fans out over, whether this attempt refuses, fails, or hangs. That
// is what makes a kill point reachable on demand.
import { llmStep, writeStep, StepRegistry } from '../../app/jobs/registry.js';
import type { StepGraph } from '../../domain/jobs/graph.js';
import { DurabilityUnproven } from '../../domain/jobs/refusal.js';

const RETRY = { attempts: 3, initialMs: 10, coefficient: 2, capMs: 100 };
/** Short enough for a test to sit through a cold-cell deadline. */
const GATE_MS = 4_000;

const LLM = { name: 'probe-llm', kind: 'llm', retry: RETRY, status: 'computing' } as const;
const WRITE = { name: 'probe-write', kind: 'write', retry: RETRY, fanout: { mode: 'per-item', from: 'probe-llm' }, status: 'applying' } as const;

export const PROBE_GRAPH: StepGraph = {
  kind: 'Probe',
  partial: null,
  doneStatus: 'done',
  nodes: [LLM, WRITE],
};

export const PROBE_GATE_GRAPH: StepGraph = {
  kind: 'ProbeGate',
  partial: null,
  doneStatus: 'done',
  nodes: [
    LLM,
    {
      name: 'probe-gate',
      kind: 'gate',
      retry: RETRY,
      status: 'awaiting_apply',
      gate: {
        events: ['apply', 'reject'],
        deadlineMs: GATE_MS,
        refreshOnEvent: false,
        onEvent: { apply: { transient: 'applying', go: 'proceed' }, reject: { transient: 'rejecting', go: 'reject' } },
        onDeadline: 'reject',
      },
    },
    WRITE,
  ],
};

export const PROBE_GRAPHS: Readonly<Record<string, StepGraph>> = { Probe: PROBE_GRAPH, ProbeGate: PROBE_GATE_GRAPH };

interface ProbeReply {
  items?: unknown[];
  refuse?: boolean;
  fail?: string;
}

export function registerProbe(registry: StepRegistry): void {
  registry.register(
    'probe-llm',
    llmStep(async (ctx) => {
      const control = String(ctx.input['control'] ?? '');
      const res = await fetch(`${control}?key=${encodeURIComponent(ctx.stepKey)}`, { signal: ctx.signal });
      if (!res.ok) throw new Error(`probe control answered ${res.status}`);
      const reply = (await res.json()) as ProbeReply;
      if (reply.refuse) throw new DurabilityUnproven('celld output gate: durability unproven: probe');
      if (reply.fail) throw new Error(reply.fail);
      const items = reply.items ?? [];
      return { value: items, items, progress: { total: items.length } };
    }),
  );

  registry.register(
    'probe-write',
    writeStep(async (ctx) => {
      const deck = ctx.repos.decks.getOrCreate(String(ctx.input['deckName'] ?? 'probe'));
      const existing = ctx.repos.idempotency.findQuestion(ctx.stepKey);
      if (existing !== null) return { value: existing, progress: { inserted: ctx.item + 1, rows: ctx.repos.questions.listInDeck(deck).length } };
      const prompt = String(ctx.itemInput ?? `probe ${ctx.item}`);
      // `add` transacts on its own, and a cell's storage refuses a nested
      // transaction: the key is recorded beside it, not around it.
      const qid = ctx.repos.questions.add(deck, { type: 'short', prompt, answer: prompt });
      ctx.repos.idempotency.recordQuestion(ctx.stepKey, qid);
      // The owner's own count, read where the rows are: what an
      // exactly-once assertion needs after a kill.
      return { value: qid, progress: { inserted: ctx.item + 1, rows: ctx.repos.questions.listInDeck(deck).length } };
    }),
  );
}
