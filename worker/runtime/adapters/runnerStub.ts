// The durable-job runner lands in phase 4 (docs/PHASE-3.md G). Until then
// every start refuses, and the use cases take the branch Python takes when
// no tier funds a workflow.
import { RunnerUnavailable, type WorkflowRunner } from '../../app/ports.js';

export class StubWorkflowRunner implements WorkflowRunner {
  async start(workflowType: string): Promise<{ workflowId: string }> {
    throw new RunnerUnavailable(`${workflowType} workflows are not available on this deploy`);
  }
}
