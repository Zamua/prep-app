// The non-terminal workflow count behind the masthead badge's label.

export interface WorkflowBadgeContext {
  workflows?: Array<{ is_terminal?: unknown }> | null;
}

export interface WorkflowBadgeFields {
  active_workflow_count: number;
}

export function deriveWorkflowBadge(context: WorkflowBadgeContext): WorkflowBadgeFields {
  const workflows = context.workflows ?? [];
  return { active_workflow_count: workflows.filter((w) => !w.is_terminal).length };
}
