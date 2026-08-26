// The trivia surfaces: the single card a push deep-links to, the
// mini-session whose queue rides in the URL, and the per-deck settings the
// notif-edit popover posts to.
import { formatDone, flipDoneVerdict, parseCardIds, parseDone, type DoneItem } from '../../domain/trivia.js';
import { pyStrip } from '../../domain/py.js';
import { DurationError, parseUntil } from '../durations.js';
import type { Question } from '../entities.js';
import { badRequest, notFound } from '../errors.js';
import { CHAT_PROVIDERS, DEFAULT_PROVIDER, buildMessage, providerUrls, quoteAll } from '../study/handoff.js';
import { page, redirect, redirectBack, type PageRequest, type PageResult } from '../pageResult.js';
import type { AgentPort, UserRepos, WorkflowRunner } from '../ports.js';
import { pyInt } from '../decks/pages.js';
import { aiRegrade, gradeWithFallback, type Verdict } from './grading.js';

export const DECK_NOT_FOUND = 'deck not found';
export const QUESTION_NOT_FOUND = 'question not found';
export const NOT_IN_DECK = 'question not found in this deck';
export const TRIVIA_DECK_NOT_FOUND = 'trivia deck not found';

export interface TriviaDeps {
  repos: UserRepos;
  agent: AgentPort;
  runner: WorkflowRunner;
}

/** A native-browser search, so an iOS PWA escapes its in-app webview. */
export const googleSearchUrl = (query: string): string => `https://www.google.com/search?q=${quoteAll(query)}`;

/** The "Explore further" block a graded card carries: chat hand-offs plus
 * a plain search. */
export function exploreContext(input: { deckName: string; q: Question; userAnswer: string; correct: boolean; idk?: boolean }): Record<string, unknown> {
  const message = buildMessage({
    deckName: input.deckName,
    q: { type: 'short', prompt: input.q.prompt, answer: input.q.answer },
    userAnswer: input.userAnswer,
    verdict: { result: input.correct ? 'right' : 'wrong' },
    idk: input.idk ?? false,
  });
  return {
    handoff_urls: providerUrls(message),
    handoff_providers: CHAT_PROVIDERS,
    handoff_default_provider: DEFAULT_PROVIDER,
    google_search_url: googleSearchUrl(input.q.prompt),
  };
}

function deckIdOf(repos: UserRepos, name: string): number {
  const id = repos.decks.findId(name);
  if (id === null) throw notFound(DECK_NOT_FOUND);
  return id;
}

const sessionUrl = (deckName: string, cards: string, done: string): string =>
  `/trivia/session/${deckName}?cards=${cards}${done ? `&done=${done}` : ''}`;

// ---- the mini-session ------------------------------------------------------

export async function triviaSession(req: PageRequest, deps: TriviaDeps): Promise<PageResult> {
  const { repos } = deps;
  const deckName = req.params['deck_name']!;
  const id = deckIdOf(repos, deckName);
  const cardsParam = req.query.get('cards');
  const doneParam = req.query.get('done') ?? '';

  if (cardsParam === null) {
    // No URL state: resume the persisted session, else pick a fresh queue.
    const active = repos.trivia.getActiveSessionForDeck(id);
    if (active && active.queue.length) {
      return redirect(sessionUrl(deckName, active.queue.join(','), active.done.length ? formatDone(active.done as DoneItem[]) : ''));
    }
    const targetSize = repos.decks.getTriviaSessionSize(id);
    // Half fresh, half review. Refilling first is synchronous by design:
    // the user just tapped a notification and is waiting to study.
    const freshTarget = Math.max(1, Math.floor(targetSize / 2));
    if (repos.trivia.countUnanswered(id) < freshTarget) {
      const topic = pyStrip(repos.decks.getContextPrompt(deckName) || deckName);
      if (topic) {
        try {
          await deps.runner.start('TriviaGenerate', { deckId: id, deckName, topic });
        } catch {
          // No refill: the session runs on what the queue already holds.
        }
      }
    }
    const picked = repos.trivia.pickSessionForDeck(id, { targetSize, freshTarget }).map((c) => c.question_id);
    await repos.trivia.replaceActive(id, { queue: picked });
    return redirect(sessionUrl(deckName, picked.join(','), ''));
  }

  const queue = parseCardIds(cardsParam);
  const doneItems = parseDone(doneParam);
  // Mid-session: keep the persisted row in step with the URL.
  if (queue.length) await repos.trivia.startOrResume(id, { queue, done: doneItems });
  if (!queue.length) {
    repos.trivia.completeSession(id);
    const results: Record<string, unknown>[] = [];
    for (const [qid, verdict] of doneItems) {
      const q = repos.questions.get(qid);
      if (q === null) continue;
      results.push({ id: q.id, prompt: q.prompt, answer: q.answer, explanation: q.explanation, verdict });
    }
    return page('trivia/session_done.html', {
      deck_name: deckName,
      results,
      right_count: results.filter((r) => r['verdict'] === 'r').length,
      total: results.length,
    });
  }

  const head = queue[0]!;
  const q = repos.questions.get(head);
  if (q === null || q.deck_id !== id) {
    // A stale URL after a delete, or an injected foreign id: pop and go on.
    return redirect(sessionUrl(deckName, queue.slice(1).join(','), doneParam));
  }
  return page('trivia/card.html', {
    q,
    deck_name: deckName,
    result: null,
    session_position: doneItems.length + 1,
    session_total: doneItems.length + queue.length,
    session_remaining: cardsParam,
    session_done: doneParam,
  });
}

export async function triviaSessionAnswer(req: PageRequest, deps: TriviaDeps): Promise<PageResult> {
  const { repos } = deps;
  const deckName = req.params['deck_name']!;
  const id = deckIdOf(repos, deckName);
  const cardsParam = req.form.get('cards') ?? '';
  const doneParam = req.form.get('done') ?? '';
  const answer = req.form.get('answer') ?? '';
  const isIdk = Boolean(req.form.get('idk'));

  const queue = parseCardIds(cardsParam);
  const doneItems = parseDone(doneParam);
  if (!queue.length) return redirect(`/trivia/session/${deckName}?cards=${doneParam ? `&done=${doneParam}` : ''}`);

  const head = queue[0]!;
  const q = repos.questions.get(head);
  if (q === null || q.deck_id !== id) return redirect(sessionUrl(deckName, queue.slice(1).join(','), doneParam));

  let verdict: Verdict;
  let given: string;
  if (isIdk) {
    // The "I don't know" submit skips grading and records a miss.
    verdict = { correct: false, feedback: null, regex_update: null };
    given = '';
  } else {
    verdict = await gradeWithFallback(deps.agent, q, answer);
    given = answer;
  }

  repos.trivia.markAnswered(head, verdict.correct);
  const regexUpdated = verdict.regex_update ? repos.questions.setAnswerRegex(head, verdict.regex_update) : false;

  const newDone: DoneItem[] = [...doneItems, [head, verdict.correct ? 'r' : 'w']];
  const remaining = queue.slice(1);
  repos.trivia.persistState(id, { queue: remaining, done: newDone });
  return page('trivia/card.html', {
    q,
    deck_name: deckName,
    result: { correct: verdict.correct, given, expected: q.answer, feedback: verdict.feedback, idk: isIdk, regex_updated: regexUpdated },
    // The counter stays on the card just answered, so it rolls up.
    session_position: doneItems.length + 1,
    session_total: doneItems.length + queue.length,
    session_remaining: remaining.join(','),
    session_done: formatDone(newDone),
    ...exploreContext({ deckName, q, userAnswer: given, correct: verdict.correct, idk: isIdk }),
  });
}

export function triviaSessionAbandon(req: PageRequest, deps: TriviaDeps): PageResult {
  const id = deckIdOf(deps.repos, req.params['deck_name']!);
  deps.repos.trivia.abandonAllSessionsForDeck(id);
  return redirect('/');
}

export function triviaSessionSnooze(req: PageRequest, deps: TriviaDeps): PageResult {
  const id = deckIdOf(deps.repos, req.params['deck_name']!);
  const preset = pyStrip(req.form.get('preset') ?? '').toLowerCase();
  if (preset === 'wake') {
    deps.repos.trivia.snoozeActiveForDeck(id, null);
    return redirect('/');
  }
  deps.repos.trivia.snoozeActiveForDeck(id, until(req, preset));
  return redirect('/');
}

export function triviaSessionOverride(req: PageRequest, deps: TriviaDeps): PageResult {
  const { repos } = deps;
  const deckName = req.params['deck_name']!;
  const id = deckIdOf(repos, deckName);
  const qid = Number(req.form.get('question_id'));
  const q = repos.questions.get(qid);
  if (q === null || q.deck_id !== id) throw notFound(NOT_IN_DECK);

  repos.trivia.setLastCorrectness(qid, true);
  const doneItems = parseDone(req.form.get('done') ?? '');
  const cardsParam = req.form.get('cards') ?? '';
  const answer = req.form.get('answer') ?? '';
  return page('trivia/card.html', {
    q,
    deck_name: deckName,
    result: { correct: true, given: answer, expected: q.answer, feedback: null, overridden: true },
    session_position: Math.max(1, doneItems.length),
    session_total: doneItems.length + parseCardIds(cardsParam).length,
    session_remaining: cardsParam,
    session_done: flipDoneVerdict(doneItems, qid, true),
    ...exploreContext({ deckName, q, userAnswer: answer, correct: true }),
  });
}

export async function triviaSessionRegrade(req: PageRequest, deps: TriviaDeps): Promise<PageResult> {
  const { repos } = deps;
  const deckName = req.params['deck_name']!;
  const id = deckIdOf(repos, deckName);
  const qid = Number(req.form.get('question_id'));
  const q = repos.questions.get(qid);
  if (q === null || q.deck_id !== id) throw notFound(NOT_IN_DECK);

  const answer = req.form.get('answer') ?? '';
  const verdict = await aiRegrade(deps.agent, { prompt: q.prompt, expected: q.answer, given: answer, currentRegex: q.answer_regex });
  repos.trivia.setLastCorrectness(qid, verdict.correct);
  const regexUpdated = verdict.regex_update ? repos.questions.setAnswerRegex(qid, verdict.regex_update) : false;

  const doneItems = parseDone(req.form.get('done') ?? '');
  const cardsParam = req.form.get('cards') ?? '';
  return page('trivia/card.html', {
    q,
    deck_name: deckName,
    result: { correct: verdict.correct, given: answer, expected: q.answer, feedback: verdict.feedback, regraded: true, regex_updated: regexUpdated },
    session_position: Math.max(1, doneItems.length),
    session_total: doneItems.length + parseCardIds(cardsParam).length,
    session_remaining: cardsParam,
    session_done: flipDoneVerdict(doneItems, qid, verdict.correct),
    ...exploreContext({ deckName, q, userAnswer: answer, correct: verdict.correct }),
  });
}

// ---- one card, outside a session -------------------------------------------

function cardAndDeck(repos: UserRepos, req: PageRequest): { q: Question; deckName: string } {
  const q = repos.questions.get(Number(req.params['question_id']));
  if (q === null) throw notFound(QUESTION_NOT_FOUND);
  return { q, deckName: repos.decks.findName(q.deck_id) ?? '' };
}

export function triviaCard(req: PageRequest, deps: TriviaDeps): PageResult {
  const { q, deckName } = cardAndDeck(deps.repos, req);
  return page('trivia/card.html', { q, deck_name: deckName, result: null });
}

export async function triviaAnswer(req: PageRequest, deps: TriviaDeps): Promise<PageResult> {
  const { repos } = deps;
  const { q, deckName } = cardAndDeck(repos, req);
  const answer = req.form.get('answer') ?? '';
  const verdict = await gradeWithFallback(deps.agent, q, answer);
  repos.trivia.markAnswered(q.id, verdict.correct);
  const regexUpdated = verdict.regex_update ? repos.questions.setAnswerRegex(q.id, verdict.regex_update) : false;
  return page('trivia/card.html', {
    q,
    deck_name: deckName,
    result: { correct: verdict.correct, given: answer, expected: q.answer, feedback: verdict.feedback, regex_updated: regexUpdated },
    ...exploreContext({ deckName, q, userAnswer: answer, correct: verdict.correct }),
  });
}

export async function triviaCardRegrade(req: PageRequest, deps: TriviaDeps): Promise<PageResult> {
  const { repos } = deps;
  const { q, deckName } = cardAndDeck(repos, req);
  const answer = req.form.get('answer') ?? '';
  const verdict = await aiRegrade(deps.agent, { prompt: q.prompt, expected: q.answer, given: answer, currentRegex: q.answer_regex });
  // Only the verdict column moves: the card keeps its place in the queue.
  repos.trivia.setLastCorrectness(q.id, verdict.correct);
  const regexUpdated = verdict.regex_update ? repos.questions.setAnswerRegex(q.id, verdict.regex_update) : false;
  return page('trivia/card.html', {
    q,
    deck_name: deckName,
    result: { correct: verdict.correct, given: answer, expected: q.answer, feedback: verdict.feedback, regraded: true, regex_updated: regexUpdated },
    ...exploreContext({ deckName, q, userAnswer: answer, correct: verdict.correct }),
  });
}

export function triviaCardOverride(req: PageRequest, deps: TriviaDeps): PageResult {
  const { repos } = deps;
  const { q, deckName } = cardAndDeck(repos, req);
  const answer = req.form.get('answer') ?? '';
  // No regex is learned: one "I was right" tap must not relax the card.
  repos.trivia.setLastCorrectness(q.id, true);
  return page('trivia/card.html', {
    q,
    deck_name: deckName,
    result: { correct: true, given: answer, expected: q.answer, feedback: null, overridden: true },
    ...exploreContext({ deckName, q, userAnswer: answer, correct: true }),
  });
}

// ---- per-deck settings -----------------------------------------------------

function until(req: PageRequest, preset?: string): string {
  try {
    return parseUntil({ preset: preset ?? req.form.get('preset'), custom: req.form.get('custom'), unit: req.form.get('unit'), now: new Date(req.now) });
  } catch (e) {
    if (e instanceof DurationError) throw badRequest(e.message);
    throw e;
  }
}

export function triviaDeckMute(req: PageRequest, deps: TriviaDeps): PageResult {
  const id = Number(req.params['deck_id']);
  const at = until(req);
  if (!deps.repos.decks.muteNotificationsUntil(id, at)) throw notFound(DECK_NOT_FOUND);
  return redirect('/');
}

export function triviaDeckUnmute(req: PageRequest, deps: TriviaDeps): PageResult {
  const id = Number(req.params['deck_id']);
  if (!deps.repos.decks.muteNotificationsUntil(id, null)) throw notFound(DECK_NOT_FOUND);
  return redirect('/');
}

/** The three notif-edit routes share one response shape: an htmx caller
 * swaps the popover, everyone else is sent back where they came from. */
function notifEdit(req: PageRequest, repos: UserRepos, deckId: number): PageResult {
  if (req.htmx) return page('partials/notif_edit.html', { deck_meta: repos.decks.getMeta(deckId) });
  return redirectBack(`/deck/${repos.decks.findName(deckId) ?? ''}`);
}

export function triviaDeckNotifications(req: PageRequest, deps: TriviaDeps, setEnabled: (deckId: number, enabled: boolean) => boolean): PageResult {
  const id = Number(req.params['deck_id']);
  if (!setEnabled(id, req.form.get('enabled') === 'on')) throw notFound(TRIVIA_DECK_NOT_FOUND);
  return notifEdit(req, deps.repos, id);
}

export function triviaDeckInterval(req: PageRequest, deps: TriviaDeps): PageResult {
  const minutes = pyInt(req.form.get('minutes') ?? '');
  if (minutes === null) throw badRequest('interval must be an integer (minutes)');
  if (minutes < 1 || minutes > 720) throw badRequest('interval must be between 1 and 720 minutes');
  const id = Number(req.params['deck_id']);
  if (!deps.repos.decks.setNotificationInterval(id, minutes)) throw notFound(TRIVIA_DECK_NOT_FOUND);
  return notifEdit(req, deps.repos, id);
}

export function triviaDeckSessionSize(req: PageRequest, deps: TriviaDeps): PageResult {
  const size = pyInt(req.form.get('size') ?? '');
  if (size === null) throw badRequest('session size must be an integer');
  if (size < 1 || size > 20) throw badRequest('session size must be between 1 and 20 cards');
  const id = Number(req.params['deck_id']);
  if (!deps.repos.decks.setTriviaSessionSize(id, size)) throw notFound(TRIVIA_DECK_NOT_FOUND);
  return notifEdit(req, deps.repos, id);
}
