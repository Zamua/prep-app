// The `WorkflowRunner` over one JobCell per job. `status` never leaves the
// calling cell: it reads `job_progress`, which the JobCell wrote through the
// status port. Only `start`, `signal` and `terminate` touch a JobCell, and
// each is one small write there.
import { jobRoute, WORKFLOW_TYPE } from '../../app/jobs/graph.js';
import { deliverJobStatus } from '../../app/jobs/status.js';
import type { NotifyDeps } from '../../app/notify/routes.js';
import type { Clock, JobCells, JobInputs, JobKind, JobStatus, JobStatusWrite, JobTransition, Random, WorkflowRunner } from '../../app/ports.js';
import { gradeId, planId, SUFFIX_HEX_CHARS, transformId, triviaId } from '../../domain/jobs/ids.js';
import { isoUtc } from '../../domain/time.js';
import { hex } from './random.js';

export interface RunnerDeps {
  jobs: JobCells;
  /** The calling cell: its repositories, its push. */
  notify: NotifyDeps;
  owner: string;
  random: Random;
  clock: Clock;
}

export class AlarmLedgerRunner implements WorkflowRunner {
  constructor(private readonly deps: RunnerDeps) {}

  async start<K extends JobKind>(kind: K, input: JobInputs[K]): Promise<{ workflowId: string }> {
    const record = input as unknown as Record<string, unknown>;
    const id = workflowId(kind, record, hex(this.deps.random.bytes(SUFFIX_HEX_CHARS / 2)));
    const route = jobRoute(kind, id, record);
    const status = await this.deps.jobs.cell(id).start({
      id,
      kind,
      owner: this.deps.owner,
      input: record,
      urlPath: route.urlPath,
      workflowType: WORKFLOW_TYPE[kind],
      deckId: route.deckId,
      deckName: route.deckName,
      at: isoUtc(this.deps.clock.now()),
    });
    await this.apply(id, kind, record, status);
    return { workflowId: id };
  }

  async signal(id: string, event: { name: string; payload?: unknown }): Promise<JobStatus | null> {
    const status = await this.deps.jobs.cell(id).signal({ name: event.name, payload: event.payload, at: isoUtc(this.deps.clock.now()) });
    if (status === null) return null;
    const stored = this.deps.notify.repos.jobs.get(id);
    if (stored) {
      await deliverJobStatus(this.deps.notify, {
        jobId: id,
        transition: status.transition,
        status: status.status,
        progress: status.progress,
        urlPath: stored.url_path,
        kind: stored.workflow_type,
        deckId: stored.deck_id,
        deckName: stored.deck_name,
      });
    }
    return this.deps.notify.repos.jobProgress.get(id) ?? status;
  }

  async status(id: string): Promise<JobStatus | null> {
    return this.deps.notify.repos.jobProgress.get(id);
  }

  async terminate(id: string, reason: string): Promise<void> {
    await this.deps.jobs.cell(id).terminate(reason, isoUtc(this.deps.clock.now()));
  }

  /**
   * The transition the JobCell just wrote, applied here rather than waited
   * for: the JobCell cannot call back into this cell while this cell is mid
   * request. Its outbox row stays undelivered and the alarm re-delivers it,
   * which the `(jobId, transition)` guard drops.
   */
  private async apply(id: string, kind: JobKind, input: Record<string, unknown>, status: JobTransition): Promise<void> {
    if (!status.status) return;
    const route = jobRoute(kind, id, input);
    const write: JobStatusWrite = {
      jobId: id,
      transition: status.transition,
      status: status.status,
      progress: status.progress,
      urlPath: route.urlPath,
      kind: WORKFLOW_TYPE[kind],
      deckId: route.deckId,
      deckName: route.deckName,
    };
    await deliverJobStatus(this.deps.notify, write);
  }
}



function workflowId(kind: JobKind, input: Record<string, unknown>, suffix: string): string {
  const deck = String(input['deckName'] ?? '');
  if (kind === 'PlanGenerate') return planId(deck, suffix);
  if (kind === 'TriviaGenerate') return triviaId(deck, suffix);
  if (kind === 'Transform') return transformId(String(input['scope']), Number(input['targetId']), suffix);
  return gradeId(deck, Number(input['questionId']), suffix);
}
