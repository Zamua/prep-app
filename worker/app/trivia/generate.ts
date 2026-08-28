// Batch generation for a trivia deck: the manual start and the polling page
// the deck-creation flow already redirects to. Both drive one
// `TriviaGenerate` job; the cell never holds an LLM call open, because a
// cell serves one request at a time and a batch takes minutes.
import { AppError, badRequest, notFound } from '../errors.js';
import { json } from '../http.js';
import { flatten } from '../jobs/view.js';
import { requireFundedWorkflow } from '../agent/funding.js';
import { triviaStartInput } from '../jobs/startInput.js';
import { agentAvailable } from '../pageContext.js';
import { page, redirect, type PageRequest, type PageResult } from '../pageResult.js';
import type { UserRepos, WorkflowRunner } from '../ports.js';
import { NO_FUNDING, parseIntLiteral } from '../decks/pages.js';

export const TRIVIA_GEN_PARTIAL = 'partials/trivia_generating_progress.html';

const MALFORMED = 'malformed trivia workflow id';
const DECK_NOT_FOUND = 'deck not found';

export interface TriviaGenerateDeps {
  runner: WorkflowRunner;
  freeTierConfigured: boolean;
}

/** `trivia-<deck_name>-<hex>`, the name taken from the left of the suffix. */
export function parseTriviaWid(wid: string): string | null {
  if (!wid.startsWith('trivia-')) return null;
  const rest = wid.slice('trivia-'.length);
  const cut = rest.lastIndexOf('-');
  if (cut < 0) return null;
  const name = rest.slice(0, cut);
  if (!name || rest.length - cut - 1 < 6) return null;
  return name;
}

/**
 * The deck and the progress a page or fragment renders. A job whose row is
 * gone reads as `done` rather than `gone`: the cards it inserted are in the
 * deck either way, and the partial has no `gone` state to render.
 */
async function loadProgress(repos: UserRepos, deps: TriviaGenerateDeps, wid: string): Promise<{ deckName: string; progress: Record<string, unknown> }> {
  const deckName = parseTriviaWid(wid);
  if (!deckName) throw badRequest(MALFORMED);
  if (repos.decks.findId(deckName) === null) throw notFound(DECK_NOT_FOUND);
  const status = await deps.runner.status(wid);
  const progress = status === null ? { status: 'done' } : flatten(status);
  progress['deck_name'] = deckName;
  return { deckName, progress };
}

export async function triviaGenView(repos: UserRepos, req: PageRequest, deps: TriviaGenerateDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const { deckName, progress } = await loadProgress(repos, deps, wid);
  return page('trivia/generating.html', { wid, deck_name: deckName, progress });
}

export async function triviaGenStatus(repos: UserRepos, req: PageRequest, deps: TriviaGenerateDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  return json((await loadProgress(repos, deps, wid)).progress);
}

export async function triviaGenFragment(repos: UserRepos, req: PageRequest, deps: TriviaGenerateDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const { deckName, progress } = await loadProgress(repos, deps, wid);
  return page(TRIVIA_GEN_PARTIAL, { wid, deck_name: deckName, progress });
}

/**
 * The manual refill button and the scheduler's fallback. `find_name` scopes
 * to this cell, so a wrong deck id is the same 404 as no such deck.
 */
export async function triviaGenerate(repos: UserRepos, req: PageRequest, deps: TriviaGenerateDeps): Promise<PageResult> {
  const deckId = parseIntLiteral(req.params['deck_id'] ?? '');
  const deckName = deckId === null ? null : repos.decks.findName(deckId);
  if (deckId === null || deckName === null) throw notFound(DECK_NOT_FOUND);
  if (!agentAvailable(repos, deps.freeTierConfigured)) throw new AppError(403, NO_FUNDING);
  // A trivia deck's context prompt is its topic; a deck without one falls
  // back to its own name.
  const topic = repos.decks.getContextPrompt(deckName) || deckName;
  try {
    requireFundedWorkflow(repos, deps.freeTierConfigured);
    const { workflowId } = await deps.runner.start('TriviaGenerate', triviaStartInput(repos, deckId, deckName, topic, deps.freeTierConfigured));
    return redirect(`/trivia/gen/${workflowId}`);
  } catch (e) {
    throw new AppError(500, `failed to start trivia workflow: ${e instanceof Error ? e.message : String(e)}`);
  }
}
