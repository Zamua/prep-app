// The transform surface: the three starts (deck, reorganize, card), the
// polling page and its fragment, and the two gate signals. The preview the
// partial renders is built here, because it needs the live rows the model
// proposed to change, not just the plan the model returned.
import { requireFundedWorkflow } from '../agent/funding.js';
import { AppError, badRequest, notFound } from '../errors.js';
import { json } from '../http.js';
import { agentAvailable } from '../pageContext.js';
import { page, redirect, type PageRequest, type PageResult } from '../pageResult.js';
import type { Question } from '../entities.js';
import type { UserRepos, WorkflowRunner } from '../ports.js';
import { pyStrip } from '../../domain/py.js';
import { flatten, gone } from '../jobs/view.js';
import { NO_FUNDING, pyInt } from './pages.js';
import { deckContextFor, transformSnapshot } from '../jobs/transform.js';


export const TRANSFORM_PARTIAL = 'partials/transform_progress.html';

const MALFORMED = 'malformed workflow id';
const TRANSFORM_NOT_FOUND = 'transform not found';
const SCOPES: readonly string[] = ['card', 'deck', 'reorganize'];

export interface TransformDeps {
  runner: WorkflowRunner;
  freeTierConfigured: boolean;
}

/** `transform-<scope>-<target_id>-<hex>`; reorganize carries the literal 0
 * because it spans every deck. */
export function parseTransformWid(wid: string): { scope: string; targetId: number } | null {
  if (!wid.startsWith('transform-')) return null;
  const parts = wid.slice('transform-'.length).split('-');
  if (parts.length < 3) return null;
  const scope = parts[0]!;
  if (!SCOPES.includes(scope)) return null;
  const targetId = pyInt(parts[1]!);
  if (targetId === null) return null;
  return { scope, targetId };
}

/** Reorganize has no single target, so the gate is that the caller's own
 * cell answered at all; card and deck scope check the row they name. */
function requireOwnsTransform(repos: UserRepos, wid: string): { scope: string; targetId: number } {
  const parsed = parseTransformWid(wid);
  if (!parsed) throw badRequest(MALFORMED);
  if (parsed.scope === 'card' && repos.questions.get(parsed.targetId) === null) throw notFound(TRANSFORM_NOT_FOUND);
  if (parsed.scope === 'deck' && repos.decks.findName(parsed.targetId) === null) throw notFound(TRANSFORM_NOT_FOUND);
  return parsed;
}

// ---- the diff view model ---------------------------------------------------

type Fields = Record<string, string>;

export interface ModificationDiff {
  question_id: number;
  deck_name: string;
  old: Fields;
  new: Fields;
}

export interface TransformViewCtx {
  deck_name: string;
  modification_diffs: ModificationDiff[];
  deletion_decks: Record<string, string>;
  move_source_decks: Record<string, string>;
  deck_id_to_name: Record<string, string>;
}

const s = (v: unknown): string => (v === null || v === undefined ? '' : String(v));

function oldFields(q: Question): Fields {
  return {
    type: q.type,
    topic: s(q.topic),
    prompt: q.prompt,
    answer: q.answer,
    rubric: s(q.rubric),
    skeleton: s(q.skeleton),
    language: s(q.language),
    explanation: s(q.explanation),
    answer_regex: s(q.answer_regex),
  };
}

/** The model's value when it sent one, else the live value, so the template
 * can walk one key set and align the two columns. Python's `or` for the
 * first four and `is not None` for the rest, transcribed: an empty string
 * clears a field the model may legitimately want emptied. */
function newFields(m: Record<string, unknown>, old: Question): Fields {
  const kept = (key: string, fallback: string): string => (m[key] === null || m[key] === undefined ? fallback : s(m[key]));
  return {
    type: s(m['type']) || old.type,
    topic: s(m['topic']) || s(old.topic),
    prompt: s(m['prompt']) || old.prompt,
    answer: s(m['answer']) || old.answer,
    rubric: kept('rubric', s(old.rubric)),
    skeleton: kept('skeleton', s(old.skeleton)),
    language: kept('language', s(old.language)),
    explanation: kept('explanation', s(old.explanation)),
    answer_regex: kept('answer_regex', s(old.answer_regex)),
  };
}

const asRecord = (v: unknown): Record<string, unknown> => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const asArray = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

export function buildTransformViewCtx(repos: UserRepos, opts: { scope: string; targetId: number; progress: Record<string, unknown> | null }): TransformViewCtx {
  const plan = asRecord(opts.progress?.['plan']);
  const deckIdToName: Record<string, string> = {};
  for (const d of repos.decks.listSummaries()) deckIdToName[String(d.id)] = d.name;

  let deckName = '';
  if (opts.scope === 'deck') {
    deckName = repos.decks.findName(opts.targetId) ?? '';
  } else {
    const q = repos.questions.get(opts.targetId);
    if (q !== null) deckName = repos.decks.findName(q.deck_id) ?? '';
  }

  const deckForQid = (qid: unknown): string => {
    const q = repos.questions.get(Number(qid));
    return q === null ? '' : (deckIdToName[String(q.deck_id)] ?? '');
  };

  const modificationDiffs: ModificationDiff[] = [];
  for (const raw of asArray(plan['modifications'])) {
    const m = asRecord(raw);
    const qid = m['question_id'];
    if (!qid) continue;
    const old = repos.questions.get(Number(qid));
    if (old === null) continue;
    modificationDiffs.push({
      question_id: Number(qid),
      deck_name: deckIdToName[String(old.deck_id)] ?? '',
      old: oldFields(old),
      new: newFields(m, old),
    });
  }

  const deletionDecks: Record<string, string> = {};
  for (const qid of asArray(plan['deletions'])) deletionDecks[String(qid)] = deckForQid(qid);

  const moveSourceDecks: Record<string, string> = {};
  for (const raw of asArray(plan['card_moves'])) {
    const qid = asRecord(raw)['question_id'];
    if (!qid) continue;
    moveSourceDecks[String(qid)] = deckForQid(qid);
  }

  return {
    deck_name: deckName,
    modification_diffs: modificationDiffs,
    deletion_decks: deletionDecks,
    move_source_decks: moveSourceDecks,
    deck_id_to_name: deckIdToName,
  };
}

// ---- the polling surface ---------------------------------------------------

async function progressOf(deps: TransformDeps, wid: string): Promise<Record<string, unknown> | null> {
  const status = await deps.runner.status(wid);
  return status === null ? null : flatten(status);
}

function fragmentOf(repos: UserRepos, wid: string, parsed: { scope: string; targetId: number }, progress: Record<string, unknown>): PageResult {
  const ctx = buildTransformViewCtx(repos, { ...parsed, progress });
  return page(TRANSFORM_PARTIAL, { wid, scope: parsed.scope, target_id: parsed.targetId, progress, ...ctx });
}

export async function transformView(repos: UserRepos, req: PageRequest, deps: TransformDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const parsed = requireOwnsTransform(repos, wid);
  const progress = (await progressOf(deps, wid)) ?? gone();
  const ctx = buildTransformViewCtx(repos, { ...parsed, progress });
  // `desc` was Temporal's workflow description; the ledger's own row is the
  // only status now, and the template reads it off `progress`.
  return page('transform.html', { wid, scope: parsed.scope, target_id: parsed.targetId, progress, desc: {}, status: progress['status'], ...ctx });
}

export async function transformStatus(repos: UserRepos, req: PageRequest, deps: TransformDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  requireOwnsTransform(repos, wid);
  return json({ progress: await progressOf(deps, wid), desc: {} });
}

export async function transformFragment(repos: UserRepos, req: PageRequest, deps: TransformDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  const parsed = requireOwnsTransform(repos, wid);
  return fragmentOf(repos, wid, parsed, (await progressOf(deps, wid)) ?? gone());
}

/** The gate signal, then the fragment for the status it produced: the
 * transient `applying` / `rejecting` renders on this response rather than
 * on whichever poll happens to catch it. */
async function signalled(repos: UserRepos, wid: string, parsed: { scope: string; targetId: number }, deps: TransformDeps, name: string): Promise<PageResult> {
  let progress: Record<string, unknown>;
  try {
    const status = await deps.runner.signal(wid, { name });
    progress = status === null ? gone() : flatten(status);
  } catch (e) {
    throw new AppError(500, `signal failed: ${message(e)}`);
  }
  return fragmentOf(repos, wid, parsed, progress);
}

export async function transformApply(repos: UserRepos, req: PageRequest, deps: TransformDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  return signalled(repos, wid, requireOwnsTransform(repos, wid), deps, 'apply');
}

export async function transformReject(repos: UserRepos, req: PageRequest, deps: TransformDeps): Promise<PageResult> {
  const wid = req.params['wid'] ?? '';
  return signalled(repos, wid, requireOwnsTransform(repos, wid), deps, 'reject');
}

// ---- the three starts ------------------------------------------------------

export async function deckTransform(repos: UserRepos, req: PageRequest, deps: TransformDeps): Promise<PageResult> {
  const name = req.params['name'] ?? '';
  const prompt = pyStrip(req.form.get('prompt') ?? '');
  if (!prompt) throw badRequest('empty prompt');
  // The deck is materialised first, as Python does: a transform names a
  // deck that may not have rows yet.
  const deckId = repos.decks.getOrCreate(name);
  if (!agentAvailable(repos, deps.freeTierConfigured)) throw new AppError(403, NO_FUNDING);
  try {
    requireFundedWorkflow(repos, deps.freeTierConfigured);
    const { workflowId } = await deps.runner.start('Transform', {
      scope: 'deck',
      targetId: deckId,
      prompt,
      deckName: name,
      deckContextPrompt: deckContextFor(repos, deckId),
      ...transformSnapshot(repos, 'deck', deckId),
    });
    return redirect(`/transform/${workflowId}`);
  } catch (e) {
    throw new AppError(500, `failed to start transform: ${message(e)}`);
  }
}

function reorganizePage(repos: UserRepos, opts: { prompt?: string; error?: string | null; status?: number } = {}): PageResult {
  const summaries = [...repos.decks.listSummaries()].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const decks = summaries.map((d) => ({
    name: d.name,
    deck_type: d.deck_type,
    total: d.total,
    topic: (d.deck_type === 'trivia' ? repos.decks.getContextPrompt(d.name) : null) ?? '',
  }));
  return page('reorganize.html', { decks, form: { prompt: opts.prompt ?? '' }, error: opts.error ?? null }, opts.status);
}

export function reorganizeForm(repos: UserRepos): PageResult {
  return reorganizePage(repos);
}

export async function reorganizeSubmit(repos: UserRepos, req: PageRequest, deps: TransformDeps): Promise<PageResult> {
  const prompt = pyStrip(req.form.get('prompt') ?? '');
  if (!prompt) throw badRequest('empty prompt');
  // Unlike the deck and card starts, an unfunded reorganize re-renders its
  // own form with the refusal rather than throwing the error page.
  if (!agentAvailable(repos, deps.freeTierConfigured)) return reorganizePage(repos, { prompt, error: NO_FUNDING, status: 403 });
  try {
    requireFundedWorkflow(repos, deps.freeTierConfigured);
    const { workflowId } = await deps.runner.start('Transform', {
      scope: 'reorganize',
      targetId: 0,
      prompt,
      deckName: null,
      // Cross-deck: each deck's JSON carries its own topic, so no single
      // deck's context applies.
      deckContextPrompt: '',
      ...transformSnapshot(repos, 'reorganize', 0),
    });
    return redirect(`/transform/${workflowId}`);
  } catch (e) {
    throw new AppError(500, `failed to start reorganize: ${message(e)}`);
  }
}

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));
