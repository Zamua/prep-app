// The AI grading poll. The verdict is written to `job_progress` by the same
// transaction that made the job terminal, so a terminal status with no
// result is a job that genuinely produced none: Python's bounded wait on
// `handle.result()` has nothing left to wait for.
import { flatten, TERMINAL_GRADING } from '../jobs/view.js';
import { json, type ApiResult } from '../http.js';
import type { UserRepos } from '../ports.js';
import { notFoundResult, pollUrl, sessionPayload, studyError, verdictOutcome, type StudyDeps } from './api.js';

/** `grade-<deck>-q<qid>-<hex>` -> [deckName, questionId], walking from the
 * right since deck names may contain hyphens. */
export function parseGradingWid(wid: string): [string, number] | null {
  if (!wid.startsWith('grade-')) return null;
  const parts = wid.slice('grade-'.length).split('-');
  if (parts.length < 3) return null;
  const qidPart = parts[parts.length - 2]!;
  if (!qidPart.startsWith('q')) return null;
  const digits = qidPart.slice(1);
  if (!/^[+-]?\d+$/.test(digits)) return null;
  return [parts.slice(0, -2).join('-'), Number(digits)];
}

const asRecord = (v: unknown): Record<string, unknown> => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

/** Releases the session before answering: left in `grading` it would hand
 * every later read another pending screen for a verdict that never lands. */
function failed(repos: UserRepos, wid: string, sid: string, note: string): ApiResult {
  if (sid) repos.sessions.gradingAbandoned(sid, wid);
  return json({ failed: { code: 'grading_failed', message: note || 'the grader returned nothing' } });
}

export async function gradingPoll(deps: StudyDeps, wid: string, sid: string): Promise<ApiResult> {
  const { repos } = deps;
  const parsed = parseGradingWid(wid);
  if (!parsed) return studyError(400, 'malformed_workflow_id', 'workflow id is not a grading id');
  const [, qid] = parsed;
  // The wid embeds the question id, so ownership is checked here or not at
  // all: guessing a wid must not expose another user's grade.
  const q = repos.questions.get(qid);
  if (q === null) return notFoundResult('question');

  const status = await deps.runner.status(wid);
  if (status === null) return failed(repos, wid, sid, '');
  const progress = flatten(status);
  const note = String(progress['error'] ?? '');

  if (!TERMINAL_GRADING.includes(status.status)) {
    // The step writes `progress.error` while still running (the busy
    // free-tier "add your own key" pointer arrives this way), so it travels
    // with the pending payload or the user never learns why the grade is slow.
    const pending: Record<string, unknown> = { poll: pollUrl(wid, sid), workflow_id: wid, status: status.status };
    if (note) pending['error'] = note;
    return json({ pending });
  }

  const result = progress['result'] === undefined || progress['result'] === null ? null : asRecord(progress['result']);
  if (result === null) return failed(repos, wid, sid, note);

  const verdict = asRecord(result['verdict']);
  const raw = asRecord(result['state']);
  const state = { step: raw['step'], next_due: raw['next_due'], interval_minutes: raw['interval_minutes'] };

  let session: Record<string, unknown> | null = null;
  if (sid) {
    // Idempotent: a second poll once the row is already showing-result is
    // a no-op, which is what makes repeat polls safe.
    repos.sessions.gradingCompleted(sid, qid, verdict, state, wid);
    const s = repos.sessions.get(sid);
    if (s !== null) session = sessionPayload(s, repos.decks.findName(s.deck_id) ?? '');
  }
  return json(verdictOutcome(q, verdict, state, String(result['user_answer'] ?? ''), Boolean(result['idk']), session));
}
