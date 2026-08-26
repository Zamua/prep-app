// Where a step's body lives. The runner imports this and never a handler, so
// a workflow is added by registering names, not by opening a runner file.
//
// A node's kind decides where its handler runs: `llm` in the JobCell, which
// holds the agent and nothing else; `write` in the owner's cell, which holds
// the repositories. A gate has no handler at all - the runner resolves it
// from the ledger.
import type { StepGraph, StepKind } from '../../domain/jobs/graph.js';
import type { StepOutput } from '../../domain/jobs/ledger.js';
import type { AgentPort, Clock, UserRepos } from '../ports.js';

export type { StepOutput };

export interface StepInfo {
  jobId: string;
  kind: string;
  owner: string;
  /** The idempotency key; a write handler passes it to the owner's ledgers. */
  stepKey: string;
  name: string;
  /** The node's position among the graph's active nodes. */
  idx: number;
  /** The fanout ordinal, 0 for a node that does not fan out. */
  item: number;
  input: Readonly<Record<string, unknown>>;
  /** Each finished node's `value` by name; a fanout node's is an array by item. */
  outputs: Readonly<Record<string, unknown>>;
  /** The item of `fanout.from` this row covers, or null off a fanout node. */
  itemInput: unknown;
  clock: Clock;
}

export interface LlmStepContext extends StepInfo {
  site: 'job';
  agent: AgentPort;
  /** Bounded by the deploy's fetch ceiling; a timeout is a step failure. */
  signal: AbortSignal;
}

export interface WriteStepContext extends StepInfo {
  site: 'owner';
  repos: UserRepos;
}

export type StepContext = LlmStepContext | WriteStepContext;
export type StepHandler = (ctx: StepContext) => Promise<StepOutput>;

export class DuplicateStep extends Error {}
export class UnknownStep extends Error {}
export class StepSiteMismatch extends Error {}

export class StepRegistry {
  private readonly handlers = new Map<string, StepHandler>();

  /** Throws on a second registration: two handlers for one node name is a
   * merge accident, and the loser would be silently unreachable. */
  register(name: string, handler: StepHandler): this {
    if (this.handlers.has(name)) throw new DuplicateStep(`${name} is already registered`);
    this.handlers.set(name, handler);
    return this;
  }

  has(name: string): boolean {
    return this.handlers.has(name);
  }

  get(name: string): StepHandler {
    const handler = this.handlers.get(name);
    if (!handler) throw new UnknownStep(`no handler registered for ${name}`);
    return handler;
  }

  names(): string[] {
    return [...this.handlers.keys()].sort();
  }
}

const sited =
  <C extends StepContext>(site: C['site']) =>
  (fn: (ctx: C) => Promise<StepOutput>): StepHandler =>
  async (ctx) => {
    if (ctx.site !== site) throw new StepSiteMismatch(`${ctx.name} ran at ${ctx.site}, not ${site}`);
    return fn(ctx as C);
  };

/** Narrows once at registration so a handler body is not full of guards. */
export const llmStep = sited<LlmStepContext>('job');
export const writeStep = sited<WriteStepContext>('owner');

export const SITE_OF: Readonly<Record<StepKind, StepContext['site'] | null>> = { llm: 'job', write: 'owner', gate: null };

/** Node names with no handler. Empty is the invariant the app boots on. */
export function unregisteredSteps(graphs: Readonly<Record<string, StepGraph>>, registry: StepRegistry): string[] {
  const out = new Set<string>();
  for (const graph of Object.values(graphs)) {
    for (const node of graph.nodes) {
      if (node.kind === 'gate') continue;
      if (!registry.has(node.name)) out.add(node.name);
    }
  }
  return [...out].sort();
}
