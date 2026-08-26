// A deploy with jobs off: every start refuses and the use cases take the
// branch Python takes when no tier funds a workflow. Reads answer as if the
// workflow were gone, which is what the partials already render.
import { RunnerUnavailable, type JobInputs, type JobKind, type JobStatus, type WorkflowRunner } from '../../app/ports.js';

export class StubWorkflowRunner implements WorkflowRunner {
  async start<K extends JobKind>(kind: K, _input: JobInputs[K]): Promise<{ workflowId: string }> {
    throw new RunnerUnavailable(`${kind} workflows are not available on this deploy`);
  }

  async signal(): Promise<JobStatus | null> {
    return null;
  }

  async status(): Promise<JobStatus | null> {
    return null;
  }

  async terminate(): Promise<void> {}
}
