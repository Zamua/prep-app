// The JSON study API, transcribed from prep/study/api.py. Shapes mirror
// the CardSource contract the browser components drive:
//   next   -> {card, draft} | {caughtUp}
//   submit -> {verdict, ...} | {selfGrade} | {pending}
//   author -> {card}
// A 200 body carries exactly one outcome key; a non-2xx body is
// {error: {code, message}}.
import { grade, GradingError, UnsupportedQuestionType } from '../../domain/grading/index.js';
import { JsonDecodeError } from '../../domain/grading/pyjson.js';
import { deviceLabelFromUa } from '../../domain/study/device.js';
import { gradeCard } from '../../domain/jobs/snapshot.js';
import { DurationError, parseUntil } from '../durations.js';
import type { CardState, Question, StudySession } from '../entities.js';
import { json, type ApiResult } from '../http.js';
import { AgentUnavailable, RunnerUnavailable, SessionNotFound, StaleVersionError, type Clock, type UserRepos, type WorkflowRunner } from '../ports.js';
import { requireFundedWorkflow } from '../agent/funding.js';
import { missing, RequestValidationError } from '../validation.js';
import { buildMessage, DEFAULT_PROVIDER, providerLabels, providerUrls } from './handoff.js';

// Free-text types have no deterministic grader: they go to the judge
// when an agent is configured, else to self-grade.
const FREE_TEXT: readonly string[] = ['code', 'short'];
const INBOX_DECK = 'inbox';

export interface StudyDeps {
  repos: UserRepos;
  clock: Clock;
  userAgent: string | null;
  /** Whether any tier funds an LLM judge for this user. */
  agentAvailable: boolean;
  runner: WorkflowRunner;
  /** Whether the shared tier would fund the judge; the start's precondition. */
  freeTierConfigured: boolean;
}

const error = (status: number, code: string, message: string, extra: Record<string, unknown> = {}): ApiResult =>
  json({ error: { code, message, ...extra } }, status);

const notFound = (what: string): ApiResult => error(404, 'not_found', `${what} not found`);

const stale = (e: StaleVersionError): ApiResult => error(409, 'stale_version', 'session moved on another device', { current_version: e.currentVersion });

// ---- payloads --------------------------------------------------------------

/** `topic` is dropped when unset, so the online and offline card sources
 * hand the components an identical key set. */
function dumpCard(card: Record<string, unknown>): Record<string, unknown> {
  if (card['topic'] === null || card['topic'] === undefined) delete card['topic'];
  return card;
}

export function cardPayload(q: Question): Record<string, unknown> {
  return dumpCard({
    question_id: q.id,
    deck_id: q.deck_id,
    type: q.type,
    prompt: q.prompt,
    choices: q.choices && q.choices.length ? q.choices : null,
    skeleton: q.skeleton,
    language: q.language,
    topic: q.topic,
  });
}

export function revealedPayload(q: Question): Record<string, unknown> {
  return dumpCard({
    question_id: q.id,
    deck_id: q.deck_id,
    type: q.type,
    prompt: q.prompt,
    choices: q.choices && q.choices.length ? q.choices : null,
    skeleton: q.skeleton,
    language: q.language,
    topic: q.topic,
    answer: q.answer,
    rubric: q.rubric,
  });
}

/** Chosen and correct option lists for mcq/multi, empty for every other type. */
function pickedCorrect(q: Question, userAnswer: string, idk: boolean): [string[], string[]] {
  if (q.type !== 'mcq' && q.type !== 'multi') return [[], []];
  const blank = idk || !userAnswer;
  let picked: unknown;
  let correct: unknown;
  try {
    if (q.type === 'multi') {
      picked = blank ? [] : JSON.parse(userAnswer);
      correct = q.answer ? JSON.parse(q.answer) : [];
    } else {
      picked = blank ? [] : [userAnswer];
      correct = q.answer ? [q.answer] : [];
    }
  } catch {
    return [[], []];
  }
  if (!Array.isArray(picked) || !Array.isArray(correct)) return [[], []];
  return [picked.map((p) => String(p)), correct.map((c) => String(c))];
}

function handoff(q: Question, opts: { deckName: string; verdict: Record<string, unknown>; userAnswer: string; idk: boolean }): Record<string, unknown> {
  const [picked, correct] = pickedCorrect(q, opts.userAnswer, opts.idk);
  const message = buildMessage({
    deckName: opts.deckName,
    q: { type: q.type, prompt: q.prompt, answer: q.answer, rubric: q.rubric, choices_list: q.choices ?? [] },
    userAnswer: opts.userAnswer,
    verdict: opts.verdict,
    idk: opts.idk,
    pickedSet: picked,
    correctSet: correct,
  });
  return { message, urls: providerUrls(message), providers: providerLabels(), default: DEFAULT_PROVIDER };
}

export function sessionPayload(s: StudySession, deckName: string): Record<string, unknown> {
  return { id: s.id, version: s.version, status: s.status, state: s.state, deck_id: s.deck_id, deck_name: deckName };
}

export function verdictOutcome(
  q: Question,
  verdict: Record<string, unknown>,
  state: Record<string, unknown>,
  userAnswer: string,
  idk: boolean,
  session: Record<string, unknown> | null,
  deckName = '',
): Record<string, unknown> {
  return {
    verdict: verdict['result'] ?? null,
    feedback: verdict['feedback'] || '',
    nextDueMinutes: state['interval_minutes'] ?? null,
    idk,
    answer: userAnswer,
    card: revealedPayload(q),
    session,
    handoff: handoff(q, { deckName, verdict, userAnswer, idk }),
  };
}

function caughtUp(deps: StudyDeps, deckId: number, session: Record<string, unknown> | null): Record<string, unknown> {
  return { caughtUp: { nextDueMinutes: deps.repos.cards.nextDueMinutes(deckId) }, session };
}

export const pollUrl = (wid: string, sid = ''): string => (sid ? `/api/study/grading/${wid}?sid=${sid}` : `/api/study/grading/${wid}`);

// ---- request bodies --------------------------------------------------------

const asObject = (body: unknown): Record<string, unknown> => (typeof body === 'object' && body !== null && !Array.isArray(body) ? (body as Record<string, unknown>) : {});

export interface SubmitBody {
  question_id: number;
  version: number | null;
  answer: string;
  idk: boolean;
  verdict: string | null;
}

function parseSubmit(body: unknown): SubmitBody {
  const o = asObject(body);
  if (o['question_id'] === undefined || o['question_id'] === null) throw new RequestValidationError([missing(['body', 'question_id'], body)]);
  return {
    question_id: Number(o['question_id']),
    version: o['version'] === undefined || o['version'] === null ? null : Number(o['version']),
    answer: typeof o['answer'] === 'string' ? (o['answer'] as string) : '',
    idk: Boolean(o['idk']),
    verdict: typeof o['verdict'] === 'string' ? (o['verdict'] as string) : null,
  };
}

function parseVersion(body: unknown): number {
  const o = asObject(body);
  if (o['version'] === undefined || o['version'] === null) throw new RequestValidationError([missing(['body', 'version'], body)]);
  return Number(o['version']);
}

// ---- session view ----------------------------------------------------------

/** What the client should render for this session right now: a card, a
 * verdict already waiting, an in-flight grade, or caught-up. */
function sessionView(deps: StudyDeps, s: StudySession): ApiResult {
  const { repos } = deps;
  const deckName = repos.decks.findName(s.deck_id) || '';
  let payload = sessionPayload(s, deckName);

  if (s.status === 'abandoned') return json({ ended: { reason: 'abandoned' }, session: payload });
  if (s.status === 'completed') return json(caughtUp(deps, s.deck_id, payload));

  if (s.state === 'showing-result') {
    const q = s.last_answered_qid ? repos.questions.get(s.last_answered_qid) : null;
    if (q === null) return notFound('question');
    const userAnswer = repos.reviews.getLastUserAnswer(q.id) || '';
    return json(verdictOutcome(q, s.last_answered_verdict ?? {}, s.last_answered_state ?? {}, userAnswer, userAnswer === '', payload, deckName));
  }

  if (s.state === 'grading') {
    const wid = s.current_grading_workflow_id || '';
    return json({ pending: { poll: pollUrl(wid, s.id), workflow_id: wid }, session: payload });
  }

  const q = s.current_question_id ? repos.questions.get(s.current_question_id) : null;
  if (q === null) {
    // No due card left: the same synchronous bump the HTML view does, so
    // both surfaces agree on the row.
    repos.sessions.markCompleted(s.id);
    const done = repos.sessions.get(s.id);
    payload = done ? sessionPayload(done, deckName) : payload;
    return json(caughtUp(deps, s.deck_id, payload));
  }
  return json({ card: cardPayload(q), draft: s.current_draft || q.skeleton || '', session: payload });
}

// ---- submission ------------------------------------------------------------

async function submit(
  deps: StudyDeps,
  body: SubmitBody,
  opts: { q: Question; deckName: string; s: StudySession | null },
): Promise<ApiResult> {
  const { repos } = deps;
  const { q, deckName, s } = opts;
  const idk = Boolean(body.idk);
  const session = s !== null ? sessionPayload(s, deckName) : null;
  if (s !== null && body.version === null) return error(422, 'version_required', 'session submissions must carry the version');

  // A self-grade decision arrives after the reveal, so it is already a
  // verdict: record it like any deterministic one.
  if (body.verdict !== null) {
    if (body.verdict !== 'right' && body.verdict !== 'wrong') return error(422, 'invalid_verdict', "verdict must be 'right' or 'wrong'");
    return record(deps, body, { q, verdict: { result: body.verdict, feedback: '(self-graded)' }, userAnswer: body.answer, idk: false, s });
  }

  const userAnswer = idk ? '' : body.answer;

  if (FREE_TEXT.includes(q.type) && !idk) {
    const selfGrade = json({ selfGrade: true, answer: userAnswer, card: revealedPayload(q), session });
    if (!deps.agentAvailable) return selfGrade;
    let wid: string;
    try {
      requireFundedWorkflow(repos, deps.freeTierConfigured);
      const started = await deps.runner.start('GradeAnswer', {
        questionId: q.id,
        deckName,
        userAnswer,
        idk,
        sessionId: s ? s.id : '',
        card: gradeCard({ type: q.type, prompt: q.prompt, answer: q.answer, rubric: q.rubric ?? '' }),
      });
      wid = started.workflowId;
      if (s !== null) repos.sessions.setGrading(s.id, q.id, wid, body.version as number);
      repos.jobs.register({
        workflowId: wid,
        workflowType: 'grading',
        deckId: null,
        deckName,
        urlPath: s !== null ? `/grading/${wid}?sid=${s.id}` : `/grading/${wid}`,
        initialStatus: 'grading',
      });
    } catch (e) {
      if (e instanceof StaleVersionError) return stale(e);
      // No tier funds an LLM judge for this user: self-grade rather than
      // book a worker slot the activity cannot pay.
      if (e instanceof RunnerUnavailable || e instanceof AgentUnavailable) return selfGrade;
      return error(502, 'grading_start_failed', `failed to start grading workflow: ${e instanceof Error ? e.message : String(e)}`);
    }
    const sid = s !== null ? s.id : '';
    const moved = s !== null ? repos.sessions.get(sid) : null;
    return json({ pending: { poll: pollUrl(wid, sid), workflow_id: wid }, session: moved ? sessionPayload(moved, deckName) : session });
  }

  let verdict: Record<string, unknown>;
  try {
    verdict = grade(q as unknown as Record<string, unknown>, userAnswer, idk) as unknown as Record<string, unknown>;
  } catch (e) {
    // Python's `except ValueError`: an ungradable type, an unreadable
    // stored answer, or malformed JSON in a `multi` submission.
    if (e instanceof UnsupportedQuestionType || e instanceof GradingError || e instanceof JsonDecodeError) return error(422, 'not_gradable', e.message);
    throw e;
  }
  return record(deps, body, { q, verdict, userAnswer, idk, s });
}

/** Write a settled verdict, then reply with the recorded outcome. */
function record(
  deps: StudyDeps,
  body: SubmitBody,
  opts: { q: Question; verdict: Record<string, unknown>; userAnswer: string; idk: boolean; s: StudySession | null },
): ApiResult {
  const { repos } = deps;
  const { q, verdict, userAnswer, idk, s } = opts;
  if (s === null) {
    const state = repos.reviews.record(q.id, verdict['result'] as 'right' | 'wrong', userAnswer, String(verdict['feedback'] || ''));
    return json(verdictOutcome(q, verdict, state as unknown as Record<string, unknown>, userAnswer, idk, null));
  }
  let state: CardState;
  try {
    state = repos.reviews.record(q.id, verdict['result'] as 'right' | 'wrong', userAnswer, String(verdict['feedback'] || ''));
    repos.sessions.recordAnswerSync(s.id, q.id, body.version as number, userAnswer, verdict, state as unknown as Record<string, unknown>);
  } catch (e) {
    if (e instanceof StaleVersionError) return stale(e);
    throw e;
  }
  const moved = repos.sessions.get(s.id);
  const deckName = repos.decks.findName(s.deck_id) || '';
  return json(verdictOutcome(q, verdict, state as unknown as Record<string, unknown>, userAnswer, idk, moved ? sessionPayload(moved, deckName) : null));
}

// ---- session lifecycle ------------------------------------------------------

/** Resume the open session on this deck, or start a fresh one, and return
 * the first view. `fresh` abandons the open one first. */
export async function begin(deps: StudyDeps, name: string, body: unknown): Promise<ApiResult> {
  const { repos } = deps;
  const deckId = repos.decks.getOrCreate(name);
  if (repos.decks.getType(deckId) === 'trivia') return error(400, 'not_studiable', 'trivia decks have no study sessions');
  const fresh = Boolean(asObject(body)['fresh']);
  const existing = repos.sessions.findActiveForDeck(deckId);
  let s: StudySession | null;
  if (existing && !fresh) {
    s = existing;
  } else {
    if (existing) repos.sessions.abandon(existing.id);
    const sid = await repos.sessions.create(deckId, deviceLabelFromUa(deps.userAgent));
    s = repos.sessions.get(sid);
  }
  if (s === null) return notFound('session');
  return sessionView(deps, s);
}

export function sessionNext(deps: StudyDeps, sid: string): ApiResult {
  const s = deps.repos.sessions.get(sid);
  if (s === null) return notFound('session');
  return sessionView(deps, s);
}

export function sessionAdvance(deps: StudyDeps, sid: string, body: unknown): ApiResult {
  const version = parseVersion(body);
  const { repos } = deps;
  if (repos.sessions.get(sid) === null) return notFound('session');
  try {
    repos.sessions.advance(sid, version);
  } catch (e) {
    if (e instanceof StaleVersionError) return stale(e);
    throw e;
  }
  const moved = repos.sessions.get(sid);
  if (moved === null) return notFound('session');
  return sessionView(deps, moved);
}

export async function sessionSubmit(deps: StudyDeps, sid: string, body: unknown): Promise<ApiResult> {
  const parsed = parseSubmit(body);
  const { repos } = deps;
  const s = repos.sessions.get(sid);
  if (s === null) return notFound('session');
  const q = repos.questions.get(parsed.question_id);
  if (q === null) return notFound('question');
  return submit(deps, parsed, { q, deckName: repos.decks.findName(s.deck_id) || '', s });
}

/** Autosave the in-progress answer. Version-checked like every other mutation. */
export function sessionDraft(deps: StudyDeps, sid: string, body: unknown): ApiResult {
  const o = asObject(body);
  if (o['version'] === undefined || o['version'] === null) throw new RequestValidationError([missing(['body', 'version'], body)]);
  const draft = typeof o['draft'] === 'string' ? (o['draft'] as string) : '';
  try {
    return json({ version: deps.repos.sessions.updateDraft(sid, draft, Number(o['version'])) });
  } catch (e) {
    if (e instanceof StaleVersionError) return stale(e);
    if (e instanceof SessionNotFound) return notFound('session');
    throw e;
  }
}

export function sessionAbandon(deps: StudyDeps, sid: string): ApiResult {
  const { repos } = deps;
  const s = repos.sessions.get(sid);
  if (s === null) return notFound('session');
  repos.sessions.abandon(sid);
  const moved = repos.sessions.get(sid);
  return json({ ended: { reason: 'abandoned' }, session: sessionPayload(moved ?? s, repos.decks.findName(s.deck_id) || '') });
}

/** Hide the session from the Continue strip until it wakes; `preset=wake` clears it. */
export function sessionSnooze(deps: StudyDeps, sid: string, body: unknown): ApiResult {
  const { repos } = deps;
  const s = repos.sessions.get(sid);
  if (s === null) return notFound('session');
  const o = asObject(body);
  const preset = String(o['preset'] ?? '').trim().toLowerCase();
  let until: string | null = null;
  if (preset !== 'wake') {
    try {
      until = parseUntil({ preset: preset || null, custom: o['custom'], unit: o['unit'], now: deps.clock.now() });
    } catch (e) {
      if (e instanceof DurationError) return error(400, 'invalid_duration', e.message);
      throw e;
    }
  }
  repos.sessions.snooze(sid, until);
  return json({ snoozed_until: until, session: sessionPayload(s, repos.decks.findName(s.deck_id) || '') });
}

// ---- deck-scoped study (no session) -----------------------------------------

/** One due card from the deck. A read never creates the deck, so an
 * unknown name is 404. */
export function deckNext(deps: StudyDeps, name: string): ApiResult {
  const { repos } = deps;
  const deckId = repos.decks.findId(name);
  if (deckId === null) return notFound('deck');
  if (repos.decks.getType(deckId) === 'trivia') return error(400, 'not_studiable', 'trivia decks have no study queue');
  const due = repos.cards.dueQuestions(deckId, 1);
  if (!due.length) return json(caughtUp(deps, deckId, null));
  const q = repos.questions.get(due[0]!.id);
  if (q === null) return notFound('question');
  return json({ card: cardPayload(q), draft: q.skeleton || '', session: null });
}

export async function deckSubmit(deps: StudyDeps, name: string, body: unknown): Promise<ApiResult> {
  const parsed = parseSubmit(body);
  const { repos } = deps;
  if (repos.decks.findId(name) === null) return notFound('deck');
  const q = repos.questions.get(parsed.question_id);
  if (q === null) return notFound('question');
  return submit(deps, parsed, { q, deckName: name, s: null });
}

// ---- authoring ---------------------------------------------------------------

/** A manually authored card: a `short` question due immediately, matching
 * the offline author flow. */
export function authorCard(deps: StudyDeps, body: unknown): ApiResult {
  const o = asObject(body);
  const errors = [];
  if (o['prompt'] === undefined || o['prompt'] === null) errors.push(missing(['body', 'prompt'], body));
  if (o['answer'] === undefined || o['answer'] === null) errors.push(missing(['body', 'answer'], body));
  if (errors.length) throw new RequestValidationError(errors);
  const { repos } = deps;
  const prompt = String(o['prompt']).trim();
  const answer = String(o['answer']).trim();
  if (!prompt || !answer) return error(422, 'invalid_card', 'both the front and the back are required');
  let deckId = o['deck_id'] === undefined || o['deck_id'] === null ? null : Number(o['deck_id']);
  if (deckId === null) deckId = repos.decks.getOrCreate(INBOX_DECK);
  const deckType = repos.decks.getType(deckId);
  if (deckType === null) return notFound('deck');
  if (deckType === 'trivia') return error(400, 'not_studiable', 'trivia decks are not studied from the card loop');
  const qid = repos.questions.add(deckId, { type: 'short', prompt, answer });
  const q = repos.questions.get(qid);
  if (q === null) return notFound('question');
  return json({ card: revealedPayload(q) }, 201);
}

// The grading poll lives in ./grading.ts; these are what it renders with.
export { error as studyError, notFound as notFoundResult };
