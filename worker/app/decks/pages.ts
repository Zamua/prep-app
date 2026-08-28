// The deck and question pages. Each use case parses nothing but its own
// inputs, calls the repositories or the service, and names a template or a
// redirect; the cell's route table turns that into a response.
import { MAX_DESIRED_RETENTION, MIN_DESIRED_RETENTION } from '../../domain/fsrs/index.js';
import { literal } from '../../domain/grading/literal.js';
import { parseIso } from '../../domain/time.js';
import { csvToDeck, deckToCsv, questionsForExport } from '../api/deckIo.js';
import { ankiNotesToDeck } from './anki.js';
import { buildApkg } from './ankiExport.js';
import { deckToPrepdeck, prepdeckToDeck } from './archive.js';
import {
  ARCHIVE_TOO_LARGE,
  EXPORT_TOO_LARGE,
  MAX_EXPORT_QUESTIONS,
  MAX_IMPORT_REVIEW_ROWS,
  MAX_IMPORT_ROWS,
  MAX_ZIP_ENTRY_BYTES,
  MAX_ZIP_TOTAL_BYTES,
} from './importLimits.js';
import type { Question } from '../entities.js';
import { AppError, badRequest, notFound } from '../errors.js';
import { requireFundedWorkflow } from '../agent/funding.js';
import { planStartInput, triviaStartInput } from '../jobs/startInput.js';
import { agentAvailable } from '../pageContext.js';
import { empty, page, redirect, redirectBack, type PageRequest, type PageResult } from '../pageResult.js';
import { NotAnApkg, RunnerUnavailable, ZipEntryTooLarge, type ApkgReader, type ApkgWriter, type Random, type UserRepos, type WorkflowRunner, type ZipCodec } from '../ports.js';
import { parseQuestionForm, questionFormFromEntity } from './questionForm.js';
import { RETENTION_PRESETS, parseFloatLiteral } from '../settings/srs.js';
import { addQuestion, setNotificationsEnabled, splitDeck, SplitRejected } from './service.js';
import { deckContextFor, transformSnapshot } from '../jobs/transform.js';
import { MAX_CONTEXT_PROMPT_CHARS, MAX_TOPIC_PROMPT_CHARS, uniqueSlug, validateDeckName, validateDisplayName } from './validation.js';

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
  const name = (req.form.get('name') ?? '').trim();
  const contextPrompt = (req.form.get('context_prompt') ?? '').trim();
  const action = (req.form.get('action') ?? '').trim() || 'empty';
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
    requireFundedWorkflow(repos, deps.freeTierConfigured);
    const { workflowId } = await deps.runner.start('PlanGenerate', planStartInput(repos, deckIdCreated, slug, contextPrompt, deps.freeTierConfigured));
    return redirect(`/plan/${workflowId}`);
  } catch (e) {
    // The deck row landed; only the workflow start failed, and the deck stays.
    throw new AppError(500, `deck created but failed to start plan workflow: ${message(e)}`);
  }
}

export async function deckNewTriviaCreate(repos: UserRepos, req: PageRequest, deps: DeckDeps): Promise<PageResult> {
  const name = (req.form.get('name') ?? '').trim();
  const topic = (req.form.get('topic') ?? '').trim();
  const rawInterval = (req.form.get('notification_interval_minutes') ?? '').trim() || '30';
  const rerender = (error: string, status = 400): PageResult => {
    const parsed = parseIntLiteral(rawInterval);
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
  const interval = parseIntLiteral(rawInterval);
  if (interval === null) return rerender('Notification interval must be an integer.');
  if (interval < 1 || interval > 720) return rerender('Notification interval must be 1–720 minutes.');

  const created = repos.decks.createTrivia(slug, { topic, intervalMinutes: interval, displayName: display });

  // AI is optional: with no funded tier the deck exists and cards get
  // added by hand, so the page lands on the deck rather than a poller.
  if (!agentAvailable(repos, deps.freeTierConfigured)) return redirect(`/deck/${slug}`);
  try {
    requireFundedWorkflow(repos, deps.freeTierConfigured);
    const { workflowId } = await deps.runner.start('TriviaGenerate', triviaStartInput(repos, created, slug, topic, deps.freeTierConfigured));
    // Only reached when the job cell produced no transition of its own; the
    // status write registers the row with the status the job actually reports.
    repos.jobs.register({
      workflowId,
      workflowType: 'trivia_gen',
      deckId: created,
      deckName: slug,
      urlPath: `/trivia/gen/${workflowId}`,
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
  const typed = (req.form.get('confirm') ?? '').trim();
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
  const cleaned = (req.form.get('context_prompt') ?? '').trim();
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
  return page('deck_export.html', { deck_name: name, deck_type: repos.decks.getType(id) ?? 'srs', error: null });
}

/** The hub again, carrying the one refusal it can produce. */
function exportRefusal(repos: UserRepos, name: string, id: number): PageResult {
  return page('deck_export.html', { deck_name: name, deck_type: repos.decks.getType(id) ?? 'srs', error: EXPORT_TOO_LARGE }, 413);
}

const download = (bytes: Uint8Array, contentType: string, filename: string): PageResult => ({
  bytes,
  status: 200,
  headers: { 'content-type': contentType, 'content-disposition': `attachment; filename="${filename}"`, 'cache-control': 'no-store' },
});

export function deckExportPrepdeck(repos: UserRepos, req: PageRequest, deps: { zip: ZipCodec }): PageResult {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  if (repos.questions.listInDeck(id).length > MAX_EXPORT_QUESTIONS) return exportRefusal(repos, name, id);
  const bytes = deckToPrepdeck(repos, id, deps.zip, req.now.replace('+00:00', 'Z'));
  return download(bytes, 'application/zip', `${name}.prepdeck`);
}

export async function deckExportApkg(repos: UserRepos, req: PageRequest, deps: { apkg: ApkgWriter; subject: string }): Promise<PageResult> {
  const name = req.params['name']!;
  const id = deckId(repos, name);
  const questions = questionsForExport(repos, id);
  if (questions.length > MAX_EXPORT_QUESTIONS) return exportRefusal(repos, name, id);
  const nowMs = parseIso(req.now).getTime();
  const { col, notes, cards } = buildApkg(name, questions, deps.subject, nowMs, req.now.slice(0, 10));
  return download(await deps.apkg.build(col, notes, cards), 'application/octet-stream', `${name}.apkg`);
}

// ---- the three importers ---------------------------------------------------

/** Every importer page takes the same three keys and nothing else. */
const importPage = (template: string, outcome: unknown, error: string | null, status?: number): PageResult => page(template, { outcome, error }, status);

/** The deck name a form posted, or the page re-rendered with why it is not
 * usable: an importer never leaves the user on an error page. */
function importDeckName(template: string, raw: string): { name: string } | { refusal: PageResult } {
  try {
    return { name: validateDeckName(raw) };
  } catch (e) {
    if (e instanceof AppError) return { refusal: importPage(template, null, e.detail, 400) };
    throw e;
  }
}

export function deckImportCsvForm(): PageResult {
  return importPage('deck_import_csv.html', null, null);
}

export function deckImportCsvSubmit(repos: UserRepos, req: PageRequest): PageResult {
  const template = 'deck_import_csv.html';
  if (!req.upload) return importPage(template, null, 'Pick a CSV file to upload.', 400);
  const named = importDeckName(template, (req.form.get('name') ?? '').trim());
  if ('refusal' in named) return named.refusal;
  // Undecodable bytes become replacement characters rather than a refusal:
  // a mostly-fine CSV with one bad byte still imports.
  const csvText = new TextDecoder('utf-8').decode(req.upload.bytes);
  return importPage(template, csvToDeck(repos, named.name, csvText, { rowCap: MAX_IMPORT_ROWS }), null);
}

export function deckImportPrepdeckForm(): PageResult {
  return importPage('deck_import_prepdeck.html', null, null);
}

export function deckImportPrepdeckSubmit(repos: UserRepos, req: PageRequest, deps: { zip: ZipCodec }): PageResult {
  const template = 'deck_import_prepdeck.html';
  if (!req.upload) return importPage(template, null, 'Pick a .prepdeck file to upload.', 400);
  const named = importDeckName(template, (req.form.get('name') ?? '').trim());
  if ('refusal' in named) return named.refusal;
  try {
    const outcome = prepdeckToDeck(repos, named.name, req.upload.bytes, deps.zip, {
      rowCap: MAX_IMPORT_ROWS,
      reviewRowCap: MAX_IMPORT_REVIEW_ROWS,
      maxEntryBytes: MAX_ZIP_ENTRY_BYTES,
      maxTotalBytes: MAX_ZIP_TOTAL_BYTES,
    });
    return importPage(template, outcome, null);
  } catch (e) {
    if (e instanceof ZipEntryTooLarge) return importPage(template, null, ARCHIVE_TOO_LARGE, 400);
    throw e;
  }
}

export function deckImportAnkiForm(): PageResult {
  return importPage('deck_import_anki.html', null, null);
}

export async function deckImportAnkiSubmit(repos: UserRepos, req: PageRequest, deps: { apkg: ApkgReader }): Promise<PageResult> {
  const template = 'deck_import_anki.html';
  if (!req.upload) return importPage(template, null, 'Pick an .apkg file to upload.', 400);
  const named = importDeckName(template, (req.form.get('name') ?? '').trim());
  if ('refusal' in named) return named.refusal;
  let notes;
  try {
    notes = await deps.apkg.notes(req.upload.bytes, { maxEntryBytes: MAX_ZIP_ENTRY_BYTES, maxTotalBytes: MAX_ZIP_TOTAL_BYTES });
  } catch (e) {
    if (e instanceof ZipEntryTooLarge) return importPage(template, null, ARCHIVE_TOO_LARGE, 400);
    if (e instanceof NotAnApkg) return importPage(template, null, e.message, 400);
    throw e;
  }
  return importPage(template, ankiNotesToDeck(repos, named.name, notes, { noteCap: MAX_IMPORT_ROWS }), null);
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
  const newName = (req.form.get('new_name') ?? '').trim();
  const newTopic = (req.form.get('new_topic') ?? '').trim();
  const selected = req.form
    .getAll('question_ids')
    .map((v) => parseIntLiteral(v))
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
  const raw = (req.form.get('retention') ?? '').trim().toLowerCase();
  let value: number | null = null;
  if (raw !== 'default' && raw !== 'none' && raw !== '') {
    value = parseFloatLiteral(raw);
    if (value === null) throw badRequest(`retention must be a number or 'default', got ${literal(raw)}`);
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
  const prompt = (req.form.get('prompt') ?? '').trim();
  if (!prompt) throw badRequest('empty prompt');
  if (!agentAvailable(repos, deps.freeTierConfigured)) throw new AppError(403, NO_FUNDING);
  const deckName = repos.decks.findName(q.deck_id);
  try {
    requireFundedWorkflow(repos, deps.freeTierConfigured);
    const { workflowId } = await deps.runner.start('Transform', {
      scope: 'card',
      targetId: qid,
      prompt,
      deckName,
      deckContextPrompt: deckContextFor(repos, q.deck_id),
      ...transformSnapshot(repos, 'card', qid),
    });
    repos.jobs.register({
      workflowId,
      workflowType: 'transform',
      deckId: null,
      deckName,
      urlPath: `/transform/${workflowId}`,
    });
    return redirect(`/transform/${workflowId}`);
  } catch (e) {
    throw new AppError(500, `failed to start transform: ${message(e)}`);
  }
}

// ---- shared ----------------------------------------------------------------

/** A whole-number literal; null when the text is not one. */
export function parseIntLiteral(raw: string): number | null {
  const s = raw.trim();
  return /^[+-]?\d+$/.test(s) ? Number(s) : null;
}

const pct = (x: number): string => `${Math.round(x * 100)}%`;

const message = (e: unknown): string => (e instanceof RunnerUnavailable || e instanceof Error ? e.message : String(e));
