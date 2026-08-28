// The status write, applied in the owner's cell. One transaction moves
// `active_workflows` and `job_progress` together and decides the push;
// delivery happens after it commits, because a fetch cannot run inside
// `transactionSync`.
//
// The notified stamp is written inside the transaction rather than after
// the send, so a transition redelivered mid-send is at most one push, not
// two.
import { isActionRequired, isTerminal } from '../badge/status.js';
import type { ActiveWorkflow } from '../entities.js';
import { sendToUser, type NotifyDeps } from '../notify/routes.js';
import type { JobStatusWrite, UserRepos } from '../ports.js';

export interface PendingPush {
  kind: 'action' | 'terminal';
  title: string;
  body: string;
  url: string;
  tag: string;
}

export interface StatusWriteResult {
  /** False when the transition was already applied. */
  applied: boolean;
  push: PendingPush | null;
}

/** Applies one transition. Call inside `repos.tx.sync`. */
export function applyJobStatus(repos: UserRepos, write: JobStatusWrite): StatusWriteResult {
  const stored = repos.jobProgress.transitionOf(write.jobId);
  if (stored !== null && write.transition <= stored) return { applied: false, push: null };

  // The prior status is what the rules diff against, and a row this write
  // creates has none, so the read happens before the register. Registering
  // first and then comparing to what we just wrote would silence the rules
  // for a job whose first delivered transition is already terminal.
  const before = repos.jobs.get(write.jobId);
  repos.jobs.register({
    workflowId: write.jobId,
    workflowType: write.kind,
    deckId: write.deckId,
    deckName: write.deckName,
    urlPath: write.urlPath,
    initialStatus: write.status,
  });
  const prev = repos.jobs.get(write.jobId);
  repos.jobProgress.upsert({ workflowId: write.jobId, transition: write.transition, status: write.status, progress: write.progress });
  if (prev === null) return { applied: true, push: null };
  const prevStatus = before ? before.status : '';
  if (prevStatus === write.status) return { applied: true, push: null };

  repos.jobs.updateStatus(write.jobId, write.status);
  let push: PendingPush | null = null;

  if (isActionRequired(write.status) && !isActionRequired(prevStatus) && !prev.notified_action_at) {
    push = { kind: 'action', title: TITLE.action, body: actionBody(prev, write.kind), url: prev.url_path, tag: `workflow-${write.jobId}` };
    repos.jobs.markNotified(write.jobId, 'action');
  }

  // The action push already covered "needs you", so a terminal the user
  // themselves chose is not announced twice; an unattended one still is.
  if (isTerminal(write.status) && !prev.notified_terminal_at) {
    repos.jobs.setTerminalAt(write.jobId);
    if (!prev.notified_action_at) {
      push = { kind: 'terminal', title: TITLE.terminal, body: terminalBody(prev, write.kind, write.status), url: prev.url_path, tag: `workflow-${write.jobId}` };
      repos.jobs.markNotified(write.jobId, 'terminal');
    }
  }
  return { applied: true, push };
}

/**
 * The rows in one transaction, then the push. The same function serves the
 * JobCell's outbox flush and the runner's local write after `start` and
 * `signal`, so a transition delivered twice takes the same path both times
 * and the second is a no-op.
 */
export async function deliverJobStatus(deps: NotifyDeps, write: JobStatusWrite): Promise<void> {
  const result = deps.repos.tx.sync(() => applyJobStatus(deps.repos, write));
  if (!result.push) return;
  const push = result.push;
  await sendToUser(deps, { title: push.title, body: push.body, url: push.url, source: 'workflow', tag: push.tag });
}

const TITLE = { action: 'Prep — action required', terminal: 'Prep — done' } as const;

const labelOf = (w: ActiveWorkflow, kind: string): string => w.deck_name || kind.split('_').join(' ');

function actionBody(w: ActiveWorkflow, kind: string): string {
  const label = labelOf(w, kind);
  if (kind === 'transform') return `Transform on ${label} is ready to review.`;
  if (kind === 'plan') return `Plan for ${label} is ready to review.`;
  return `${label} needs your attention.`;
}

function terminalBody(w: ActiveWorkflow, kind: string, status: string): string {
  const label = labelOf(w, kind);
  if (status === 'failed' || status === 'FAILED') return `${kind.split('_').join(' ')} on ${label} failed.`;
  if (kind === 'trivia_gen') return `Trivia for ${label} is ready.`;
  if (kind === 'grading') return `Grading is done — ${label}.`;
  if (kind === 'plan') return `Plan for ${label} is done.`;
  return `Transform on ${label} is done.`;
}

