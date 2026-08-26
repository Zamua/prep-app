// The plan-first generation surface: the polling page, its JSON status, the
// htmx fragment and the three gate signals. The workflow id is the only
// ownership evidence a deep link carries, so every entry parses it and
// checks the deck before reading anything.
import { badRequest, notFound } from '../errors.js';
import { json } from '../http.js';
import { page, redirect, type PageRequest, type PageResult } from '../pageResult.js';
import { flatten, gone } from '../jobs/view.js';
import type { UserRepos, WorkflowRunner } from '../ports.js';
import { pyStrip } from '../../domain/py.js';

export const PLAN_PARTIAL = 'partials/plan_progress.html';

const MALFORMED = 'malformed workflow id';
const PLAN_NOT_FOUND = 'plan not found';

/** `plan-<deck_name>-<hex>`. The deck name may itself contain hyphens, so
 * the suffix is taken from the right. */
export function parsePlanWid(wid: string): string | null {
  if (!wid.startsWith('plan-')) return null;
  const rest = wid.slice('plan-'.length);
  const cut = rest.lastIndexOf('-');
  if (cut < 0) return null;
  const name = rest.slice(0, cut);
  if (!name || rest.length - cut - 1 < 6) return null;
  return name;
}

export interface PlanDeps {
  runner: WorkflowRunner;
}

/** The deck the wid names, or a refusal. A guessed wid must 404 rather than
 * leak that some other account owns the job. */
function requireOwnsPlan(repos: UserRepos, wid: string): string {
  const name = parsePlanWid(wid);
  if (!name) throw badRequest(MALFORMED);
  if (repos.decks.findId(name) === null) throw notFound(PLAN_NOT_FOUND);
  return name;
}

async function progressOf(deps: PlanDeps, wid: string): Promise<Record<string, unknown> | null> {
  const status = await deps.runner.status(wid);
  return status === null ? null : flatten(status);
}

const fragment = (wid: string, deckName: string, progress: Record<string, unknown>): PageResult =>
  page(PLAN_PARTIAL, { wid, deck_name: deckName, progress });

export async function planView(repos: UserRepos, req: PageRequest, deps: PlanDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const deckName = requireOwnsPlan(repos, wid);
  const progress = await progressOf(deps, wid);
  // Nothing left to poll, so the deck page is the honest destination.
  if (progress === null) return redirect(`/deck/${deckName}`);
  return page('plan.html', { wid, deck_name: deckName, progress });
}

export async function planStatus(repos: UserRepos, req: PageRequest, deps: PlanDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  requireOwnsPlan(repos, wid);
  return json((await progressOf(deps, wid)) ?? gone());
}

export async function planFragment(repos: UserRepos, req: PageRequest, deps: PlanDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const deckName = requireOwnsPlan(repos, wid);
  return fragment(wid, deckName, (await progressOf(deps, wid)) ?? gone());
}

/** The signal answers with the status it produced, so the transient
 * `accepting` / `rejecting` / `replanning` reaches the swap without a
 * second read racing the transition it is meant to show. */
async function signalled(deps: PlanDeps, wid: string, name: string, payload?: unknown): Promise<Record<string, unknown>> {
  const status = await deps.runner.signal(wid, { name, payload });
  return status === null ? gone() : flatten(status);
}

export async function planFeedback(repos: UserRepos, req: PageRequest, deps: PlanDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const deckName = requireOwnsPlan(repos, wid);
  const feedback = pyStrip(req.form.get('feedback') ?? '');
  if (!feedback) throw badRequest('empty feedback');
  return fragment(wid, deckName, await signalled(deps, wid, 'feedback', feedback));
}

export async function planAccept(repos: UserRepos, req: PageRequest, deps: PlanDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const deckName = requireOwnsPlan(repos, wid);
  return fragment(wid, deckName, await signalled(deps, wid, 'accept'));
}

export async function planReject(repos: UserRepos, req: PageRequest, deps: PlanDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const deckName = requireOwnsPlan(repos, wid);
  return fragment(wid, deckName, await signalled(deps, wid, 'reject'));
}
