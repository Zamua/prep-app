// The deck and question pages. Each use case parses nothing but its own
// inputs, calls the repositories or the service, and names a template or a
// redirect; the cell's route table turns that into a response.
import { MAX_DESIRED_RETENTION, MIN_DESIRED_RETENTION } from '../../domain/fsrs/index.js';
import { pyRepr } from '../../domain/grading/pyrepr.js';
import { pyStrip } from '../../domain/py.js';
import { deckToCsv } from '../api/deckIo.js';
import type { Question } from '../entities.js';
import { AppError, badRequest, notFound } from '../errors.js';
import { agentAvailable } from '../pageContext.js';
import { empty, page, redirect, redirectBack, type PageRequest, type PageResult } from '../pageResult.js';
import { RunnerUnavailable, type Random, type UserRepos, type WorkflowRunner } from '../ports.js';
import { parseQuestionForm, questionFormFromEntity } from './questionForm.js';
import { RETENTION_PRESETS, pyFloat } from '../settings/srs.js';
import { addQuestion, setNotificationsEnabled, splitDeck, SplitRejected } from './service.js';
import { MAX_CONTEXT_PROMPT_CHARS, MAX_TOPIC_PROMPT_CHARS, uniqueSlug, validateDisplayName } from './validation.js';

export const DECK_NOT_FOUND = 'deck not found';
export const QUESTION_NOT_FOUND = 'question not found';

export const NO_FUNDING =
  'AI is not configured. Add a personal API key on /settings/agent, or ask the deploy admin to configure a shared tier.';

export interface DeckDeps {
  freeTierConfigured: boolean;
  random: Random;
  runner: WorkflowRunner;
}

function deckId(repos: UserRepos, name: string): number {
  const id = repos.decks.findId(name);
  if (id === null) throw notFound(DECK_NOT_FOUND);
  return id;
}

// ---- deck creation ---------------------------------------------------------

export function deckNewChooser(): PageResult {
  return page('deck_new_chooser.html', {});
}

export function deckNewSrsForm(): PageResult {
  return page('deck_new_srs.html', { name_value: '', context_value: '', error: null });
}

export function deckNewTriviaForm(): PageResult {
  return page('deck_new_trivia.html', { name_value: '', topic_value: '', interval_value: 30, error: null });
}

export async function deckNewSrsCreate(repos: UserRepos, req: PageRequest, deps: DeckDeps): Promise<PageResult> {
  const name = pyStrip(req.form.get('name') ?? '');
  const contextPrompt = pyStrip(req.form.get('context_prompt') ?? '');
  const action = pyStrip(req.form.get('action') ?? '') || 'empty';
  const rerender = (error: string, status = 400): PageResult =>
    page('deck_new_srs.html', { name_value: name, context_value: contextPrompt, error }, status);

  let display: string;
  try {
    display = validateDisplayName(name);
  } catch (e) {
    if (e instanceof AppError) return rerender(e.detail);
    throw e;
  }
  const slug = uniqueSlug(repos.decks, deps.random);

  if (contextPrompt.length > MAX_CONTEXT_PROMPT_CHARS) {
    return rerender(`Description is too long (${contextPrompt.length} chars; max ${MAX_CONTEXT_PROMPT_CHARS}).`);
  }
  if (action === 'plan') {
    if (!agentAvailable(repos, deps.freeTierConfigured)) {
      return rerender(
        "Plan & generate needs an AI agent. Configure one on the agent settings page (/settings/agent), or pick 'Create empty deck' to add cards yourself.",
      );
    }
    if (!contextPrompt) return rerender('Plan & generate needs a description for the AI to plan against.');
  }

  const deckIdCreated = repos.decks.create(slug, { contextPrompt: contextPrompt || null, displayName: display });
  if (action !== 'plan') return redirect(`/deck/${slug}`);

  try {
    const { workflowId } = await deps.runner.start('PlanGenerate', { deckId: deckIdCreated, deckName: slug, prompt: contextPrompt });
    return redirect(`/plan/${workflowId}`);
  } catch (e) {
    // The deck row landed; only the workflow start failed, and the deck stays.
    throw new AppError(500, `deck created but failed to start plan workflow: ${message(e)}`);
  }
}

export async function deckNewTriviaCreate(repos: UserRepos, req: PageRequest, deps: DeckDeps): Promise<PageResult> {
  const name = pyStrip(req.form.get('name') ?? '');
  const topic = pyStrip(req.form.get('topic') ?? '');
  const rawInterval = pyStrip(req.form.get('notification_interval_minutes') ?? '') || '30';
  const rerender = (error: string, status = 400): PageResult => {
    const parsed = pyInt(rawInterval);
    return page('deck_new_trivia.html', { name_value: name, topic_value: topic, interval_value: parsed ?? 30, error }, status);
  };

  let display: string;
  try {
    display = validateDisplayName(name);
  } catch (e) {
    if (e instanceof AppError) return rerender(e.detail);
    throw e;
  }
  const slug = uniqueSlug(repos.decks, deps.random);

  if (topic.length > MAX_CONTEXT_PROMPT_CHARS) return rerender(`Topic is too long (${topic.length} chars; max ${MAX_CONTEXT_PROMPT_CHARS}).`);
  if (!topic) {
    return rerender('Topic is required — it describes what the deck is about, and the AI uses it later if you configure one.');
  }
  const interval = pyInt(rawInterval);
  if (interval === null) return rerender('Notification interval must be an integer.');
  if (interval < 1 || interval > 720) return rerender('Notification interval must be 1–720 minutes.');

  const created = repos.decks.createTrivia(slug, { topic, intervalMinutes: interval, displayName: display });

  // AI is optional: with no funded tier the deck exists and cards get
  // added by hand, so the page lands on the deck rather than a poller.
  if (!agentAvailable(repos, deps.freeTierConfigured)) return redirect(`/deck/${slug}`);
  try {
    const { workflowId } = await deps.runner.start('TriviaGenerate', { deckId: created, deckName: slug, topic });
    repos.jobs.register({
      workflowId,
      workflowType: 'trivia_gen',
      deckId: created,
      deckName: slug,
      urlPath: `/trivia/gen/${workflowId}`,
      initialStatus: 'computing',
    });
    return redirect(`/trivia/gen/${workflowId}`);
  } catch (e) {
    throw new AppError(500, `deck created but failed to start trivia workflow: ${message(e)}`);
  }
}

// ---- one deck --------------------------------------------------------------

export function deckView(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = repos.decks.getOrCreate(name);
  const cards = repos.questions.listInDeck(id);
  const type = repos.decks.getType(id);
  const meta = repos.decks.getMeta(id);
  const isTrivia = type === 'trivia';
  const isSrs = type === 'srs';
  return page('deck.html', {
    deck_name: name,
    questions: cards,
    deck_type: type ?? 'srs',
    // The template still calls the trivia metadata `trivia`.
    trivia: meta,
    deck_meta: meta,
    trivia_stats: isTrivia ? repos.trivia.deckStats(id) : null,
    due_count: cards.filter((c) => !c.suspended && c.next_due !== null && c.next_due <= req.now).length,
    deck_retention: isSrs ? repos.decks.getDesiredRetention(id) : null,
    user_retention: isSrs ? repos.prefs.getDesiredRetention() : null,
    retention_presets: isSrs ? RETENTION_PRESETS : null,
  });
}

export function deckDelete(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  const typed = pyStrip(req.form.get('confirm') ?? '');
  const expected = repos.decks.getMeta(id).display_name || name;
  // Either label works, so a renamed deck never locks its owner out.
  if (typed !== expected && typed !== name) throw badRequest("deck name didn't match — delete not performed");
  repos.decks.delete(name);
  return redirect('/');
}

export function deckUpdateTopic(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  if (repos.decks.getType(id) !== 'trivia') throw badRequest('topic prompt only applies to trivia decks');
  const cleaned = pyStrip(req.form.get('context_prompt') ?? '');
  if (!cleaned) throw badRequest('topic prompt cannot be empty');
  if (cleaned.length > MAX_TOPIC_PROMPT_CHARS) {
    throw badRequest(`topic prompt too long (${cleaned.length} chars; max ${MAX_TOPIC_PROMPT_CHARS})`);
  }
  repos.decks.updateContextPrompt(name, cleaned);
  // The popover closes itself; there is nothing to swap.
  if (req.htmx) return empty(204);
  return redirect(`/deck/${name}`);
}

export function deckRename(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  deckId(repos, name);
  // The slug stays put so bookmarks and open MCP sessions keep working.
  repos.decks.updateDisplayName(name, validateDisplayName(req.form.get('new_name') ?? ''));
  return redirect(`/deck/${name}`);
}

export function deckEditWithAi(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  return page('deck_edit_ai.html', { deck_name: name, deck_type: repos.decks.getType(id) ?? 'srs', error: null });
}

export function deckEditWithClaude(req: PageRequest): PageResult {
  return redirect(`/deck/${req.params['name']!}/edit-with-ai`);
}

export function deckExportHub(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  return page('deck_export.html', { deck_name: name, deck_type: repos.decks.getType(id) ?? 'srs' });
}

/** The hub's CSV button. `no-store`: a download taken right after adding a
 * card has to see the new row. */
export function deckExportCsv(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  return {
    text: deckToCsv(repos, id),
    status: 200,
    headers: {
      'content-type': 'text/csv; charset=utf-8',
      'content-disposition': `attachment; filename="${name}.csv"`,
      'cache-control': 'no-store',
    },
  };
}

function splitContext(repos: UserRepos, name: string, id: number, form: { new_name: string; new_topic: string; selected_ids: number[] }, error: string | null) {
  const type = repos.decks.getType(id);
  return {
    deck_name: name,
    deck_type: type ?? 'srs',
    cards: repos.questions.listInDeck(id),
    source_topic: (type === 'trivia' ? repos.decks.getContextPrompt(name) : null) ?? '',
    error,
    form,
  };
}

export function deckSplitForm(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  return page('deck_split.html', splitContext(repos, name, id, { new_name: '', new_topic: '', selected_ids: [] }, null));
}

export async function deckSplitSubmit(repos: UserRepos, req: PageRequest): Promise<PageResult> {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  const newName = pyStrip(req.form.get('new_name') ?? '');
  const newTopic = pyStrip(req.form.get('new_topic') ?? '');
  const selected = req.form
    .getAll('question_ids')
    .map((v) => pyInt(v))
    .filter((n): n is number => n !== null);
  try {
    await splitDeck(repos, { sourceDeckId: id, newDeckName: newName, questionIds: selected, newTopicPrompt: newTopic || null });
  } catch (e) {
    if (e instanceof SplitRejected) {
      return page('deck_split.html', splitContext(repos, name, id, { new_name: newName, new_topic: newTopic, selected_ids: selected }, e.message), 400);
    }
    throw e;
  }
  return redirect(`/deck/${newName}`);
}

export function deckPin(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  const pinned = req.form.get('pinned') === 'on';
  repos.decks.setPinned(id, pinned);
  if (req.htmx) return page('partials/pin_form.html', { deck_name: name, pinned });
  return redirectBack(`/deck/${name}`);
}

export function deckRetention(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  if (repos.decks.getType(id) !== 'srs') throw badRequest('retention applies only to SRS decks');
  const raw = pyStrip(req.form.get('retention') ?? '').toLowerCase();
  let value: number | null = null;
  if (raw !== 'default' && raw !== 'none' && raw !== '') {
    value = pyFloat(raw);
    if (value === null) throw badRequest(`retention must be a number or 'default', got ${pyRepr(raw)}`);
    if (!(MIN_DESIRED_RETENTION <= value && value <= MAX_DESIRED_RETENTION)) {
      throw badRequest(`retention must be between ${pct(MIN_DESIRED_RETENTION)} and ${pct(MAX_DESIRED_RETENTION)}`);
    }
  }
  if (!repos.decks.setDesiredRetention(id, value)) throw notFound(DECK_NOT_FOUND);
  return redirect(`/deck/${name}`);
}

export function deckNotifications(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  if (!setNotificationsEnabled(repos, id, req.form.get('enabled') === 'on')) throw notFound(DECK_NOT_FOUND);
  return redirectBack(`/deck/${name}`);
}

// ---- questions -------------------------------------------------------------

export function questionNewForm(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  deckId(repos, name);
  return page('question_new.html', { deck_name: name, form: {}, error: null });
}

export function questionNewSubmit(repos: UserRepos, req: PageRequest): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  const { question, raw, error } = parseQuestionForm(req.form);
  if (error !== null || question === null) return page('question_new.html', { deck_name: name, form: raw, error }, 400);
  addQuestion(repos, id, question);
  return redirect(`/deck/${name}`);
}

function questionAndDeck(repos: UserRepos, qid: number): { q: Question; deckName: string } {
  const q = repos.questions.get(qid);
  if (q === null) throw notFound(QUESTION_NOT_FOUND);
  const deckName = repos.decks.findName(q.deck_id);
  if (deckName === null) throw notFound(DECK_NOT_FOUND);
  return { q, deckName };
}

export function questionEditForm(repos: UserRepos, req: PageRequest): PageResult {
  const { q, deckName } = questionAndDeck(repos, Number(req.params['qid']));
  return page('question_edit.html', { deck_name: deckName, q, form: questionFormFromEntity(q), error: null });
}

export function questionEditSubmit(repos: UserRepos, req: PageRequest): PageResult {
  const qid = Number(req.params['qid']);
  const { q, deckName } = questionAndDeck(repos, qid);
  const { question, raw, error } = parseQuestionForm(req.form);
  if (error !== null || question === null) return page('question_edit.html', { deck_name: deckName, q, form: raw, error }, 400);
  repos.questions.update(qid, question);
  return redirect(`/deck/${deckName}`);
}

function setSuspended(repos: UserRepos, req: PageRequest, suspended: boolean): PageResult {
  const qid = Number(req.params['qid']);
  const q = repos.questions.get(qid);
  if (q === null) throw notFound(QUESTION_NOT_FOUND);
  repos.questions.setSuspended(qid, suspended);
  // The htmx caller toggles the row's class itself; nothing to swap.
  if (req.htmx) return empty(204);
  return redirect(`/deck/${repos.decks.findName(q.deck_id) ?? ''}`);
}

export const questionSuspend = (repos: UserRepos, req: PageRequest): PageResult => setSuspended(repos, req, true);
export const questionUnsuspend = (repos: UserRepos, req: PageRequest): PageResult => setSuspended(repos, req, false);

export async function questionImprove(repos: UserRepos, req: PageRequest, deps: DeckDeps): Promise<PageResult> {
  const qid = Number(req.params['qid']);
  const q = repos.questions.get(qid);
  if (q === null) throw notFound(QUESTION_NOT_FOUND);
  const prompt = pyStrip(req.form.get('prompt') ?? '');
  if (!prompt) throw badRequest('empty prompt');
  if (!agentAvailable(repos, deps.freeTierConfigured)) throw new AppError(403, NO_FUNDING);
  const deckName = repos.decks.findName(q.deck_id);
  try {
    const { workflowId } = await deps.runner.start('Transform', { scope: 'card', targetId: qid, prompt, deckName });
    repos.jobs.register({
      workflowId,
      workflowType: 'transform',
      deckId: null,
      deckName,
      urlPath: `/transform/${workflowId}`,
      initialStatus: 'computing',
    });
    return redirect(`/transform/${workflowId}`);
  } catch (e) {
    throw new AppError(500, `failed to start transform: ${message(e)}`);
  }
}

// ---- shared ----------------------------------------------------------------

/** Python's `int()`: null when the literal would raise ValueError. */
export function pyInt(raw: string): number | null {
  const s = raw.trim();
  return /^[+-]?\d+$/.test(s) ? Number(s) : null;
}

const pct = (x: number): string => `${Math.round(x * 100)}%`;

const message = (e: unknown): string => (e instanceof RunnerUnavailable || e instanceof Error ? e.message : String(e));
