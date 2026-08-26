// The four job kinds as data. The runner reads these and nothing else, so a
// workflow's shape is reviewable as a table rather than as control flow.
// Retry values are the Go worker's RetryPolicy values, transcribed.
import type { RetryPolicy, StepGraph } from '../../domain/jobs/graph.js';

/** No retry on an LLM call: re-running a long prompt hides the real failure
 * for another long prompt, so the error reaches the user instead. */
const LLM: RetryPolicy = { attempts: 1, initialMs: 2_000, coefficient: 2, capMs: 30_000 };
const WRITE: RetryPolicy = { attempts: 3, initialMs: 1_000, coefficient: 2, capMs: 30_000 };
const TRIVIA_WRITE: RetryPolicy = { attempts: 3, initialMs: 500, coefficient: 2, capMs: 30_000 };
const RECORD: RetryPolicy = { attempts: 5, initialMs: 1_000, coefficient: 2, capMs: 30_000 };

const HOUR = 3_600_000;

export type JobKind = 'PlanGenerate' | 'Transform' | 'TriviaGenerate' | 'GradeAnswer';

export const PLAN_GRAPH: StepGraph = {
  kind: 'PlanGenerate',
  partial: 'partials/plan_progress.html',
  doneStatus: 'done',
  nodes: [
    { name: 'plan', kind: 'llm', retry: LLM, status: 'planning' },
    {
      name: 'gate',
      kind: 'gate',
      retry: LLM,
      status: 'awaiting_feedback',
      gate: {
        events: ['accept', 'reject', 'feedback'],
        deadlineMs: 24 * HOUR,
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
    { name: 'expand', kind: 'llm', retry: LLM, fanout: { mode: 'batch', size: 4, from: 'plan' }, onError: 'skip', status: 'generating', emptyError: 'every card expansion failed' },
    { name: 'insert', kind: 'write', retry: WRITE, fanout: { mode: 'per-item', from: 'expand' }, onError: 'skip', status: 'applying' },
  ],
};

export const TRANSFORM_GRAPH: StepGraph = {
  kind: 'Transform',
  partial: 'partials/transform_progress.html',
  doneStatus: 'done',
  progressFromInput: ['scope'],
  nodes: [
    { name: 'compute', kind: 'llm', retry: LLM, status: 'computing' },
    {
      name: 'gate',
      kind: 'gate',
      retry: LLM,
      status: 'awaiting_apply',
      // Card scope auto-applies: the node is not in the graph for that input.
      onlyWhen: { input: 'scope', in: ['deck', 'reorganize'] },
      gate: {
        events: ['apply', 'reject'],
        deadlineMs: HOUR,
        refreshOnEvent: false,
        onEvent: {
          apply: { transient: 'applying', go: 'proceed' },
          reject: { transient: 'rejecting', go: 'reject' },
        },
        onDeadline: 'reject',
      },
    },
    { name: 'apply', kind: 'write', retry: WRITE, status: 'applying' },
  ],
};

export const TRIVIA_GRAPH: StepGraph = {
  kind: 'TriviaGenerate',
  partial: 'partials/trivia_generating_progress.html',
  doneStatus: 'done',
  nodes: [
    { name: 'generate', kind: 'llm', retry: LLM, status: 'generating' },
    { name: 'insert', kind: 'write', retry: TRIVIA_WRITE, fanout: { mode: 'per-item', from: 'generate' }, onError: 'skip', status: 'applying' },
  ],
};

export const GRADE_GRAPH: StepGraph = {
  kind: 'GradeAnswer',
  // The grading result is read as JSON by /api/study/grading/{wid}.
  partial: null,
  doneStatus: 'done',
  nodes: [
    { name: 'grade', kind: 'llm', retry: LLM, status: 'grading' },
    { name: 'record', kind: 'write', retry: RECORD, keyIsJobId: true, status: 'recording' },
  ],
};

export const JOB_GRAPHS: Readonly<Record<JobKind, StepGraph>> = {
  PlanGenerate: PLAN_GRAPH,
  Transform: TRANSFORM_GRAPH,
  TriviaGenerate: TRIVIA_GRAPH,
  GradeAnswer: GRADE_GRAPH,
};

/** The badge's `workflow_type` for a kind, as Python spells it. */
export const WORKFLOW_TYPE: Readonly<Record<JobKind, string>> = {
  PlanGenerate: 'plan',
  Transform: 'transform',
  TriviaGenerate: 'trivia_gen',
  GradeAnswer: 'grading',
};

/** The badge row a start registers: the deep link and the deck it names.
 * Same values Python's start routes pass to `workflows.register`. */
export function jobRoute(kind: JobKind, id: string, input: Readonly<Record<string, unknown>>): { urlPath: string; deckId: number | null; deckName: string | null } {
  const deckName = typeof input['deckName'] === 'string' ? (input['deckName'] as string) : null;
  const deckId = typeof input['deckId'] === 'number' ? (input['deckId'] as number) : null;
  if (kind === 'PlanGenerate') return { urlPath: `/plan/${id}`, deckId, deckName };
  if (kind === 'TriviaGenerate') return { urlPath: `/trivia/gen/${id}`, deckId, deckName };
  if (kind === 'Transform') {
    const target = typeof input['targetId'] === 'number' ? (input['targetId'] as number) : null;
    return { urlPath: `/transform/${id}`, deckId: input['scope'] === 'deck' ? target : null, deckName };
  }
  const sid = typeof input['sessionId'] === 'string' ? (input['sessionId'] as string) : '';
  return { urlPath: sid ? `/grading/${id}?sid=${sid}` : `/grading/${id}`, deckId: null, deckName };
}
