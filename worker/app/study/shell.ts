// The study shell's own routes: the HTML pages and redirects around a
// session. The shell itself reads session state through the JSON study
// API, so every in-session branch is decided client-side from one source
// of truth; only an abandoned session redirects server-side.
import { deviceLabelFromUa } from '../../domain/study/device.js';
import { DurationError, parseUntil } from '../durations.js';
import { badRequest, notFound } from '../errors.js';
import { page, redirect, type PageRequest, type PageResult } from '../pageResult.js';
import type { UserRepos } from '../ports.js';
import { parseGradingWid } from './grading.js';

export const SESSION_NOT_FOUND = 'session not found';
export const NO_STUDY_SESSIONS = 'trivia decks are notification-driven; no study sessions';
export const NO_SUCH_JOB = 'no such grading job';
export const MALFORMED_WID = 'malformed workflow id';

export interface ShellDeps {
  repos: UserRepos;
  signInUrl: string;
}

export async function studyBegin(req: PageRequest, deps: ShellDeps): Promise<PageResult> {
  const { repos } = deps;
  const name = req.params['name']!;
  const deckId = repos.decks.getOrCreate(name);
  // A trivia deck has no `cards` rows, so a stale bookmark would open an
  // empty SRS session against it.
  if (repos.decks.getType(deckId) === 'trivia') throw badRequest(NO_STUDY_SESSIONS);
  const fresh = (req.query.get('fresh') ?? '0') !== '0';
  const existing = repos.sessions.findActiveForDeck(deckId);
  if (!fresh && existing) return redirect(`/session/${existing.id}`);
  if (fresh && existing) repos.sessions.abandon(existing.id);
  const sid = await repos.sessions.create(deckId, deviceLabelFromUa(req.userAgent));
  return redirect(`/session/${sid}`);
}

export function sessionView(req: PageRequest, deps: ShellDeps): PageResult {
  const { repos } = deps;
  const sid = req.params['sid']!;
  const s = repos.sessions.get(sid);
  if (s === null) throw notFound(SESSION_NOT_FOUND);
  const deckName = repos.decks.findName(s.deck_id) ?? '';
  if (s.status === 'abandoned') return redirect(`/deck/${deckName}`);
  return page('study_shell.html', { deck_name: deckName, session_id: sid, sign_in_url: deps.signInUrl });
}

export function sessionAbandon(req: PageRequest, deps: ShellDeps): PageResult {
  const { repos } = deps;
  const sid = req.params['sid']!;
  const s = repos.sessions.get(sid);
  repos.sessions.abandon(sid);
  const deckName = s === null ? '' : (repos.decks.findName(s.deck_id) ?? '');
  return redirect(deckName ? `/deck/${deckName}` : '/');
}

export function sessionSnooze(req: PageRequest, deps: ShellDeps): PageResult {
  const sid = req.params['sid']!;
  const preset = (req.form.get('preset') ?? '').trim().toLowerCase();
  if (preset === 'wake') {
    deps.repos.sessions.snooze(sid, null);
    return redirect('/');
  }
  let until: string;
  try {
    until = parseUntil({ preset, custom: req.form.get('custom'), unit: req.form.get('unit'), now: new Date(req.now) });
  } catch (e) {
    if (e instanceof DurationError) throw badRequest(e.message);
    throw e;
  }
  deps.repos.sessions.snooze(sid, until);
  return redirect('/');
}

/** The sessionless path: the same shell, minus a session id. */
export function studyView(req: PageRequest, deps: ShellDeps): PageResult {
  const name = req.params['name']!;
  deps.repos.decks.getOrCreate(name);
  return page('study_shell.html', { deck_name: name, session_id: null, sign_in_url: deps.signInUrl });
}

/** Links minted while grading had its own polling page. The workflow id
 * names a deck and a question and is attacker-supplied, so both must
 * belong to the caller: otherwise a crafted id would mint decks in the
 * visitor's account through /study/{name}'s get-or-create. */
export function gradingView(req: PageRequest, deps: ShellDeps): PageResult {
  const parsed = parseGradingWid(req.params['wid']!);
  if (!parsed) throw badRequest(MALFORMED_WID);
  const [deckName, qid] = parsed;
  const { repos } = deps;
  if (repos.questions.get(qid) === null) throw notFound(NO_SUCH_JOB);
  const sid = req.query.get('sid') ?? '';
  if (sid) {
    if (repos.sessions.get(sid) === null) throw notFound(NO_SUCH_JOB);
    return redirect(`/session/${sid}`);
  }
  if (repos.decks.findId(deckName) === null) throw notFound(NO_SUCH_JOB);
  return redirect(`/study/${deckName}`);
}
