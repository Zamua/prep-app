// A job kind as data: an ordered list of nodes, each naming its handler,
// its retry policy and the status literal a partial renders while it runs.
// The runner reads only this; it never names a workflow.

export interface RetryPolicy {
  /** Total attempts, including the first. 1 means no retry. */
  attempts: number;
  initialMs: number;
  coefficient: number;
  capMs: number;
}

/** Where a node's handler runs: `llm` and `gate` in the JobCell, `write` in the owner's. */
export type StepKind = 'llm' | 'write' | 'gate';

/**
 * How many rows a node materializes.
 * `per-item`: one row per item of `from`'s output.
 * `batch`: one row per item too, with a barrier every `size` rows; group
 * `n + 1` starts only once every member of group `n` has landed.
 */
export interface Fanout {
  mode: 'batch' | 'per-item';
  size?: number;
  /** The node whose output supplies the items. */
  from: string;
}

/** What resolving a gate on `event` does: the transient status written before
 * the signal RPC returns, then either past the gate, a rejection, or a re-run
 * of an earlier node that returns to the same gate. */
export interface GateOutcome {
  transient: string;
  go: 'proceed' | 'reject' | { rerun: string };
}

export interface Gate {
  events: readonly string[];
  deadlineMs: number;
  /** Always false: the Go single-timer rule. A re-run does not extend the deadline. */
  refreshOnEvent: false;
  onEvent: Readonly<Record<string, GateOutcome>>;
  /** The event the deadline stands in for. */
  onDeadline: string;
  /** Prefix for the progress error a failed re-run leaves on the gate. */
  rerunError?: string;
}

/** A node that only exists for some inputs: the transform gate, which the
 * `card` scope skips. */
export interface NodeCondition {
  input: string;
  in: readonly (string | number | boolean | null)[];
}

export interface StepNode {
  name: string;
  kind: StepKind;
  fanout?: Fanout;
  retry: RetryPolicy;
  /** `skip` is the Go "don't fail siblings" rule; the default is `fail`. */
  onError?: 'fail' | 'skip';
  gate?: Gate;
  onlyWhen?: NodeCondition;
  /** The literal the partial renders while this node is the cursor. */
  status: string;
  /** The grading record step, whose idempotency key is the job id itself. */
  keyIsJobId?: true;
  /** Failure message when every row of a fanout node ended skipped. Without
   * it an all-skipped node is simply empty and the job carries on. */
  emptyError?: string;
}

export interface StepGraph {
  kind: string;
  nodes: readonly StepNode[];
  /** The partial that renders this kind's progress, or null for a JSON-only kind. */
  partial: string | null;
  /** The status a finished run lands on. */
  doneStatus: string;
  /** Input keys copied into the progress payload before the first step runs,
   * so a partial polling at transition 1 already has them. */
  progressFromInput?: readonly string[];
}

/** Terminal statuses the runner writes; `gone` is what a missing row renders. */
export const TERMINAL_JOB_STATUSES = ['done', 'rejected', 'failed'] as const;
export type TerminalJobStatus = (typeof TERMINAL_JOB_STATUSES)[number];

export const nodeOnError = (node: StepNode): 'fail' | 'skip' => node.onError ?? 'fail';

/** The nodes this input actually runs: a node whose condition excludes it is
 * not in the graph for that job at all, so the cursor never lands on it. */
export function activeNodes(graph: StepGraph, input: Readonly<Record<string, unknown>>): StepNode[] {
  return graph.nodes.filter((n) => n.onlyWhen === undefined || n.onlyWhen.in.includes(input[n.onlyWhen.input] as never));
}

export function nodeAt(graph: StepGraph, input: Readonly<Record<string, unknown>>, cursor: number): StepNode | null {
  return activeNodes(graph, input)[cursor] ?? null;
}

/** Every status literal a run of this graph can write, for the partial check. */
export function statusLiterals(graph: StepGraph): string[] {
  const out = new Set<string>();
  for (const node of graph.nodes) {
    out.add(node.status);
    for (const outcome of Object.values(node.gate?.onEvent ?? {})) out.add(outcome.transient);
  }
  out.add(graph.doneStatus);
  out.add('failed');
  // Only a gate can reject; a linear kind never writes that status.
  if (graph.nodes.some((n) => Object.values(n.gate?.onEvent ?? {}).some((o) => o.go === 'reject'))) out.add('rejected');
  return [...out];
}

export class MalformedGraph extends Error {}

/** The invariants the runner assumes. Cheap, so the graph test runs it over
 * every declared kind rather than trusting review. */
export function validateGraph(graph: StepGraph): void {
  const bad = (msg: string): never => {
    throw new MalformedGraph(`${graph.kind}: ${msg}`);
  };
  if (graph.nodes.length === 0) bad('has no nodes');
  // One deadline column, written once: a second gate would inherit the
  // first's deadline rather than opening its own.
  if (graph.nodes.filter((n) => n.kind === 'gate').length > 1) bad('declares more than one gate');
  const seen = new Set<string>();
  graph.nodes.forEach((node, i) => {
    if (i === 0 && node.kind === 'gate') bad('opens on a gate');
    if (seen.has(node.name)) bad(`declares ${node.name} twice`);
    seen.add(node.name);
    if (!node.status) bad(`${node.name} has no status literal`);
    if (node.retry.attempts < 1) bad(`${node.name} allows ${node.retry.attempts} attempts`);
    if (node.retry.coefficient < 1) bad(`${node.name} backs off by ${node.retry.coefficient}`);
    if (node.retry.capMs < node.retry.initialMs) bad(`${node.name} caps below its initial interval`);
    if (node.kind === 'gate') {
      const gate = node.gate ?? bad(`${node.name} is a gate with no gate block`);
      if (gate.events.length === 0) bad(`${node.name} waits on no event`);
      for (const event of gate.events) {
        const outcome = gate.onEvent[event] ?? bad(`${node.name} has no outcome for ${event}`);
        if (typeof outcome.go === 'object' && !seen.has(outcome.go.rerun)) bad(`${node.name} re-runs ${outcome.go.rerun}, which is not an earlier node`);
      }
      if (!gate.events.includes(gate.onDeadline)) bad(`${node.name} deadlines to ${gate.onDeadline}, which is not one of its events`);
      if (gate.deadlineMs <= 0) bad(`${node.name} has a non-positive deadline`);
      if (node.fanout) bad(`${node.name} is a gate and cannot fan out`);
    } else if (node.gate) {
      bad(`${node.name} carries a gate block but is a ${node.kind} node`);
    }
    if (node.fanout && !seen.has(node.fanout.from)) bad(`${node.name} fans out from ${node.fanout.from}, which is not an earlier node`);
    if (node.fanout?.mode === 'batch' && !(node.fanout.size && node.fanout.size > 0)) bad(`${node.name} batches with no size`);
    if (node.keyIsJobId && node.fanout) bad(`${node.name} keys on the job id and cannot fan out`);
  });
}
