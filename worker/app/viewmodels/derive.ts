// `derive(template, context)`: the context plus the fields a template
// reads instead of computing in place. Pure; one deriver per template
// that needs one, keyed by the template name it serves.
import { deriveSettingsAgent, type SettingsAgentContext } from "./settingsAgent";
import { deriveTransformProgress, type TransformProgressContext } from "./transformProgress";
import { deriveWorkflowBadge, type WorkflowBadgeContext } from "./workflowBadge";

type Context = Record<string, unknown>;
type Deriver = (context: Context) => object;

const DERIVERS: Record<string, Deriver> = {
  "transform.html": (c) => deriveTransformProgress(c as TransformProgressContext),
  "partials/transform_progress.html": (c) => deriveTransformProgress(c as TransformProgressContext),
  "settings_agent.html": (c) => deriveSettingsAgent(c as SettingsAgentContext),
  "partials/workflow_badge.html": (c) => deriveWorkflowBadge(c as WorkflowBadgeContext),
};

export function derive(template: string, context: Context): Context {
  const deriver = DERIVERS[template];
  return deriver ? { ...context, ...deriver(context) } : { ...context };
}

export const DERIVED_TEMPLATES: readonly string[] = Object.keys(DERIVERS);
