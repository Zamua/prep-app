// Where the four workflows meet the runner: one registration call, by node
// name. The runner imports the registry, never a handler, so this file is the
// only place that knows all four exist.
import { writeStep, type StepHandler, type StepRegistry, type WriteStepContext } from './registry.js';
import { expandStep, planInsert, planStep } from './plan.js';
import { applyStep, computeStep } from './transform.js';
import { generateStep, triviaInsert } from './trivia.js';
import { gradeRecord, gradeStep } from './grade.js';

/** Two graphs name a node `insert`, and the registry is keyed by name alone,
 * so the job's kind picks the body. */
const INSERT: Record<string, (ctx: WriteStepContext) => ReturnType<StepHandler>> = {
  PlanGenerate: planInsert,
  TriviaGenerate: triviaInsert,
};

export class UnroutedStep extends Error {}

export function registerWorkflowSteps(registry: StepRegistry): void {
  registry.register('plan', planStep);
  registry.register('expand', expandStep);
  registry.register('compute', computeStep);
  registry.register('apply', writeStep(applyStep));
  registry.register('generate', generateStep);
  registry.register('grade', gradeStep);
  registry.register('record', writeStep(gradeRecord));
  registry.register(
    'insert',
    writeStep(async (ctx) => {
      const handler = INSERT[ctx.kind];
      if (!handler) throw new UnroutedStep(`no insert handler for job kind ${JSON.stringify(ctx.kind)}`);
      return handler(ctx);
    }),
  );
}
