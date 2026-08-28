// The per-user alarm: what the cell reads, and what it does with the plan
// the domain returns. There is no walk over every user: a cell wakes for its
// own.
//
// The reading and the doing are separate on purpose. `nextWakeAt` re-derives
// the wake from the rows after any write, and `runWake` reads the same rows
// through the same function, so what the alarm is armed for and what it does
// when it fires cannot drift apart.
import { formatDone, type DoneItem } from '../../domain/trivia.js';
import { isoUtc } from '../../domain/time.js';
import { MAX_BACKOFF_DOUBLINGS, planWake, type TriviaDeckState, type WakeInputs, type WakePlan, type WakeTask } from '../../domain/notify/wake.js';
import { fundingTier, requireFundedWorkflow } from '../agent/funding.js';
import { WORKFLOW_TYPE } from '../jobs/graph.js';
import { triviaStartInput } from '../jobs/startInput.js';
import type { ActiveWorkflow } from '../entities.js';
import { AgentUnavailable, RunnerUnavailable, type Clock, type UserRepos, type WorkflowRunner } from '../ports.js';
import { sendToUser, type NotifyDeps } from './routes.js';

export interface WakeDeps extends NotifyDeps {
  runner: WorkflowRunner;
  clock: Clock;
  /** Whether the shared tier would fund the refill this alarm dispatches. */
  freeTierConfigured: boolean;
  /** Whether this deploy runs jobs at all; a start refuses when it does not. */
  jobsEnabled: boolean;
}

export interface WakeReport {
  ran: string[];
  /** One entry per task that threw; its stamp is unwritten, so it retries. */
  failed: string[];
}

export const DIGEST_TITLE = 'Prep — daily digest';
export const WHEN_READY_TITLE = 'Prep — cards ready';
export const DEFAULT_SESSION_SIZE = 3;
/** The body a push service will accept; the rest is trimmed. */
const BODY_LIMIT = 240;
/** Long enough that `listForUser` returns every terminal row there is. */
const PRUNE_SCAN_SECONDS = 100 * 365 * 24 * 3600;

/** Everything the plan rests on, read in one pass. */
export function readWakeInputs(repos: UserRepos, clock: Clock, canGenerate: boolean): WakeInputs {
  const nextDueMinutes = repos.cards.nextDueMinutes(null);
  const jobs = repos.jobs.listForUser({ recentTerminalWindowSeconds: PRUNE_SCAN_SECONDS });
  const lastRefill = new Map<number, string>();
  for (const w of jobs) {
    if (w.workflow_type !== WORKFLOW_TYPE.TriviaGenerate || w.deck_id === null) continue;
    const seen = lastRefill.get(w.deck_id);
    if (seen === undefined || w.started_at > seen) lastRefill.set(w.deck_id, w.started_at);
  }
  const decks: TriviaDeckState[] = repos.decks.listTriviaDecks().map((d) => {
    const stats = repos.trivia.deckStats(d.id);
    const session = repos.trivia.getActiveSessionForDeck(d.id);
    return {
      id: d.id,
      notificationsEnabled: d.notifications_enabled,
      mutedUntil: d.notifications_muted_until,
      intervalMinutes: d.notification_interval_minutes,
      ignoredStreak: d.notification_ignored_streak,
      lastNotifiedAt: d.last_notified_at,
      sessionSize: d.trivia_session_size || DEFAULT_SESSION_SIZE,
      unanswered: stats.unanswered,
      queued: stats.total,
      topic: (d.context_prompt || d.name || '').trim(),
      lastRefillAt: lastRefill.get(d.id) ?? null,
      activeSince: session && session.queue.length ? session.last_active : null,
    };
  });
  return {
    prefs: repos.prefs.getNotificationPrefs(),
    canGenerate,
    hasPushDevice: repos.pushSubs.count() > 0,
    dueTotal: repos.cards.countDue(),
    nextDueAt: nextDueMinutes === null ? null : isoUtc(new Date(clock.now().getTime() + nextDueMinutes * 60_000)),
    decks,
    earliestTerminalAt: earliestTerminal(jobs),
  };
}

export function planFor(repos: UserRepos, clock: Clock, canGenerate: boolean): WakePlan {
  return planWake(readWakeInputs(repos, clock, canGenerate), clock.now());
}

/** Null when nothing is outstanding: the cell sleeps until a request. The
 * arm and the run read the same rows through the same function, so what the
 * alarm was set for and what it does when it fires cannot drift apart. */
export function nextWakeAt(repos: UserRepos, clock: Clock, canGenerate: boolean): string | null {
  return planFor(repos, clock, canGenerate).wakeAt;
}

/**
 * One activation. Each task carries its own guard, so a duplicate fire costs
 * a read; a task that throws leaves its stamp unwritten and is retried rather
 * than taking the rest of the plan down with it.
 */
export async function runWake(deps: WakeDeps): Promise<WakeReport> {
  const now = deps.clock.now();
  const plan = planWake(readWakeInputs(deps.repos, deps.clock, canGenerate(deps)), now);
  const ran: string[] = [];
  const failed: string[] = [];
  for (const task of plan.tasks) {
    try {
      await runTask(deps, task, now);
      ran.push(label(task));
    } catch (e) {
      failed.push(`${label(task)}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  return { ran, failed };
}

const label = (task: WakeTask): string => ('deckId' in task ? `${task.kind}:${task.deckId}` : task.kind);

async function runTask(deps: WakeDeps, task: WakeTask, now: Date): Promise<void> {
  if (task.kind === 'digest') return digest(deps, task.localDate);
  if (task.kind === 'when-ready') return whenReady(deps, now);
  if (task.kind === 'trivia-refill') return refill(deps, task.deckId);
  if (task.kind === 'trivia-notify') return notifyDeck(deps, task.deckId, now);
  prune(deps.repos);
}

// ---- the tasks ---------------------------------------------------------------

async function digest(deps: WakeDeps, localDate: string): Promise<void> {
  const total = deps.repos.cards.countDue();
  await sendToUser(deps, { title: DIGEST_TITLE, body: digestBody(deps.repos.decks.dueBreakdown(), total), url: '/', source: 'srs-digest' });
  const prefs = deps.repos.prefs.getNotificationPrefs();
  prefs.last_digest_date = localDate;
  deps.repos.prefs.setNotificationPrefs(prefs);
}

export function digestBody(breakdown: readonly [string, number][], total: number): string {
  if (total === 0) return '';
  if (breakdown.length === 1) {
    const [name, n] = breakdown[0]!;
    return `${n} card${n !== 1 ? 's' : ''} due in ${name}.`;
  }
  const head = breakdown
    .slice(0, 3)
    .map(([name, n]) => `${n} in ${name}`)
    .join(', ');
  const extra = breakdown.length <= 3 ? '' : `, + ${breakdown.length - 3} more`;
  return `${total} cards due — ${head}${extra}.`;
}

async function whenReady(deps: WakeDeps, now: Date): Promise<void> {
  const total = deps.repos.cards.countDue();
  await sendToUser(deps, {
    title: WHEN_READY_TITLE,
    body: `${total} card${total !== 1 ? 's' : ''} due to study.`,
    url: '/',
    source: 'srs-when-ready',
  });
  const prefs = deps.repos.prefs.getNotificationPrefs();
  prefs.last_when_ready_at = isoUtc(now);
  deps.repos.prefs.setNotificationPrefs(prefs);
}

/**
 * The refill is a dispatch and nothing more: the job calls the LLM in its own
 * cell, on its own alarm. A deploy that cannot start one is swallowed, and
 * the deck still gets notified with whatever it already holds.
 */
async function refill(deps: WakeDeps, deckId: number): Promise<void> {
  const deckName = deps.repos.decks.findName(deckId);
  if (deckName === null) return;
  const topic = (deps.repos.decks.getMeta(deckId).context_prompt || deckName).trim();
  if (!topic) return;
  try {
    requireFundedWorkflow(deps.repos, deps.freeTierConfigured);
    await deps.runner.start('TriviaGenerate', triviaStartInput(deps.repos, deckId, deckName, topic, deps.freeTierConfigured));
  } catch (e) {
    if (e instanceof RunnerUnavailable || e instanceof AgentUnavailable) return;
    throw e;
  }
}

async function notifyDeck(deps: WakeDeps, deckId: number, now: Date): Promise<void> {
  const repos = deps.repos;
  const deck = repos.decks.listTriviaDecks().find((d) => d.id === deckId);
  if (!deck) return;
  const picked = await pickBody(deps, deck.id, deck.name, deck.trivia_session_size || DEFAULT_SESSION_SIZE);
  if (picked === null) return;

  // Engagement decides the cadence of the push about to go out: an answer
  // since the last one resets the streak, no answer doubles it.
  const engaged = repos.trivia.hasAnswerSince(deck.id, deck.last_notified_at);
  const streak = engaged ? 0 : Math.min(deck.notification_ignored_streak + 1, MAX_BACKOFF_DOUBLINGS);
  await sendToUser(deps, {
    title: deck.name || 'Trivia',
    body: picked.body,
    url: picked.url,
    source: 'trivia',
    // Per deck, so a new push replaces the prior one instead of stacking.
    tag: deck.name ? `trivia-${deck.name}` : 'trivia',
  });
  repos.decks.recordNotificationFire(deck.id, isoSeconds(now), streak);
}

/**
 * Resume the session the user actually started, else pick a fresh one and
 * persist it, so the deep link and the body name the same cards the route
 * will render. A queue nobody answered is dropped rather than resumed.
 */
async function pickBody(deps: WakeDeps, deckId: number, deckName: string, sessionSize: number): Promise<{ body: string; url: string } | null> {
  const trivia = deps.repos.trivia;
  const active = trivia.getActiveSessionForDeck(deckId);
  if (active && active.queue.length && active.done.length && trivia.promptForQuestion(active.queue[0]!)) {
    const remaining = active.queue.length;
    const done = formatDone(active.done as DoneItem[]);
    const url = `/trivia/session/${deckName}?cards=${active.queue.join(',')}${done ? `&done=${done}` : ''}`;
    return { body: `Pick up where you left off — ${remaining} card${remaining !== 1 ? 's' : ''} remaining`, url };
  }
  const cards = trivia.pickSessionForDeck(deckId, { targetSize: sessionSize, freshTarget: Math.max(1, Math.floor(sessionSize / 2)) });
  if (!cards.length) return null;
  await trivia.replaceActive(
    deckId,
    { queue: cards.map((c) => c.question_id) },
  );
  const head = cards[0]!.prompt;
  return {
    body: head.length > BODY_LIMIT ? `${head.slice(0, BODY_LIMIT - 3)}...` : head,
    url: `/trivia/session/${deckName}?cards=${cards.map((c) => c.question_id).join(',')}`,
  };
}

function prune(repos: UserRepos): void {
  repos.jobs.pruneTerminalOlderThan();
  repos.jobProgress.pruneOrphans();
}

// ---- reads ---------------------------------------------------------------------

function earliestTerminal(jobs: readonly ActiveWorkflow[]): string | null {
  let oldest: string | null = null;
  for (const w of jobs) {
    if (w.terminal_at === null) continue;
    if (oldest === null || w.terminal_at < oldest) oldest = w.terminal_at;
  }
  return oldest;
}

/** Whether a refill is a task that could ever finish: a tier that funds the
 * call, and a deploy that accepts a start. Neither is a per-attempt
 * condition, so a plan that ignored them would leave the deck asking on every
 * wake for the life of the deploy. */
export function canGenerate(deps: { repos: UserRepos; freeTierConfigured: boolean; jobsEnabled: boolean }): boolean {
  return deps.jobsEnabled && fundingTier(deps.repos, deps.freeTierConfigured) !== 'none';
}

/** Whole seconds, which is the precision the deck column holds. */
const isoSeconds = (d: Date): string => isoUtc(new Date(Math.floor(d.getTime() / 1000) * 1000));
