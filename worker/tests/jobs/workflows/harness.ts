// The four real workflows over the real runner: lane A's cells and ledger,
// with only the model faked. A reply is a pure function of the prompt and the
// call number, so a run is reproducible and a prompt assertion is a byte
// assertion.
import { JOB_GRAPHS, WORKFLOW_TYPE, jobRoute, type JobKind } from '../../../app/jobs/graph.js';
import { registerWorkflowSteps } from '../../../app/jobs/index.js';
import type { AgentPort, AgentRequest, Clock, JobStatus, JobTransition, UserRepos } from '../../../app/ports.js';
import type { WriteStepContext } from '../../../app/jobs/registry.js';
import { composeWith } from '../../../runtime/compose.js';
import { jobHarness, seedOwner, type JobHarness } from '../harness.js';
import { PARITY_NOW, USER } from '../../repos/setup.js';

export type Reply = string | Error;
export type Script = (prompt: string, call: number) => Reply;

/** Every prompt it was sent, and a scripted reply for each. */
export class FakeAgent implements AgentPort {
  readonly prompts: string[] = [];

  constructor(private readonly script: Script) {}

  async complete(request: AgentRequest): Promise<string> {
    const call = this.prompts.length;
    this.prompts.push(request.user);
    const reply = this.script(request.user, call);
    if (reply instanceof Error) throw reply;
    return reply;
  }
}

export interface WorkflowHarness extends JobHarness {
  agent: FakeAgent;
  /** Writes the job and its first transition, as a route's `start` does. */
  start(kind: JobKind, id: string, input: Record<string, unknown>): Promise<JobTransition>;
  signal(id: string, name: string, payload?: unknown): Promise<JobTransition | null>;
  /** Fires alarms until the job parks on a gate or lands terminal. */
  run(id: string, max?: number): Promise<'gated' | 'terminal' | 'stuck'>;
  /** The status the owner's `job_progress` row renders. */
  progress(id: string): JobStatus | null;
  statuses(id: string): string[];
  job(id: string): Record<string, unknown>;
  stepKeys(id: string, name: string): string[];
  stepStatuses(id: string, name: string): string[];
}

export function workflowHarness(script: Script, at: Date = PARITY_NOW): WorkflowHarness {
  const agent = new FakeAgent(script);
  const h = jobHarness({ graphs: JOB_GRAPHS, agent, at });
  // An LLM step asks for an agent per activation, so the seam a workflow test
  // needs is the selection, not the instant endpoint's port.
  composeWith(h.env, { agentFor: () => agent });
  seedOwner(h, USER, { push: false });
  registerWorkflowSteps(h.registry);

  const rows = (id: string, name: string) =>
    h
      .ledger(id)
      .steps.filter((r) => r['name'] === name)
      .sort((a, b) => Number(a['item']) - Number(b['item']));

  return {
    ...h,
    agent,
    start: (kind, id, input) => {
      const route = jobRoute(kind, id, input);
      return h.jobCell(id).start({
        id,
        kind,
        owner: USER,
        input,
        urlPath: route.urlPath,
        workflowType: WORKFLOW_TYPE[kind],
        deckId: route.deckId,
        deckName: route.deckName,
        at: h.clock.now().toISOString(),
      });
    },
    signal: (id, name, payload) => h.jobCell(id).signal({ name, payload, at: h.clock.now().toISOString() }),
    run: async (id, max = 80) => {
      for (let i = 0; i < max; i++) {
        const job = h.ledger(id).job;
        if (job['state'] === 'terminal' && h.jobStorage(id).alarmAt === null) return 'terminal';
        // A gated cell's only wake is the deadline; ticking would spend it.
        if (job['state'] === 'gated') return 'gated';
        if (!(await h.tick(id))) return 'stuck';
      }
      return 'stuck';
    },
    progress: (id) => h.repos().jobProgress.get(id),
    statuses: (id) => h.statusWrites.filter((w) => w.jobId === id).map((w) => w.status),
    job: (id) => h.ledger(id).job,
    stepKeys: (id, name) => rows(id, name).map((r) => String(r['step_key'])),
    stepStatuses: (id, name) => rows(id, name).map((r) => String(r['status'])),
  };
}

/** A write step's context, for the assertions that need the handler alone:
 * running one twice under its key is the whole point of the key. */
export function writeCtx(opts: {
  repos: UserRepos;
  clock: Clock;
  stepKey: string;
  name: string;
  input: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  itemInput?: unknown;
  item?: number;
  kind?: string;
  jobId?: string;
}): WriteStepContext {
  return {
    site: 'owner',
    jobId: opts.jobId ?? 'job-1',
    kind: opts.kind ?? 'Transform',
    owner: USER,
    stepKey: opts.stepKey,
    name: opts.name,
    idx: 0,
    item: opts.item ?? 0,
    input: opts.input,
    outputs: opts.outputs ?? {},
    itemInput: opts.itemInput ?? null,
    clock: opts.clock,
    repos: opts.repos,
  };
}
