import { describe, expect, it } from 'vitest';
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import type { FakeCellStorage } from '../fakes/sqlStorage.js';
import { INTERNAL_TOKEN, ORIGIN, SEED_USER, seed, workerEnv } from './harness.js';

const authHeaders = {
  accept: 'application/json',
  'tailscale-user-login': SEED_USER,
  'tailscale-user-name': 'Seed',
  'x-internal-token': INTERNAL_TOKEN,
};

type StudyCard = { question_id: number; prompt: string };
type StudySession = { id: string; version: number; status: string; state: string };
type StudyView = { card: StudyCard; session: StudySession };
type VerdictView = { verdict: string; nextDueMinutes: number; session: StudySession };

async function postJson(env: Env, path: string, body: unknown, now?: string): Promise<Response> {
  return worker.fetch(
    new Request(`${ORIGIN}${path}`, {
      method: 'POST',
      headers: { ...authHeaders, 'content-type': 'application/json', ...(now ? { 'x-prep-test-now': now } : {}) },
      body: JSON.stringify(body),
    }),
    env,
  );
}

async function fixture(): Promise<{ env: Env; storage: FakeCellStorage; ids: { mcq: number; regex: number } }> {
  const state = workerEnv();
  const seeded = await seed(state.env, 'offline_e2e', SEED_USER);
  return {
    env: state.env,
    storage: state.userStorage(SEED_USER),
    ids: {
      mcq: Number(seeded['mcq_id']),
      regex: Number(seeded['regex_id']),
    },
  };
}

function cardRow(storage: FakeCellStorage, questionId: number): Record<string, unknown> {
  const row = storage.rows('cards').find((candidate) => Number(candidate['question_id']) === questionId);
  if (!row) throw new Error(`missing card ${questionId}`);
  return row;
}

function sessionRow(storage: FakeCellStorage, sessionId: string): Record<string, unknown> {
  const row = storage.rows('study_sessions').find((candidate) => String(candidate['id']) === sessionId);
  if (!row) throw new Error(`missing session ${sessionId}`);
  return row;
}

describe('the study session HTTP lifecycle', () => {
  it('records a Right answer, shows its result, and advances to the next card', async () => {
    const { env, storage, ids } = await fixture();

    const begun = await postJson(env, '/api/study/decks/offline-e2e/session', {});
    expect(begun.status).toBe(200);
    const first = (await begun.json()) as StudyView;
    expect(first.card).toMatchObject({ question_id: ids.mcq, prompt: 'Capital of France?' });
    expect(first.session).toMatchObject({ status: 'active', state: 'awaiting-answer', version: 1 });

    const submitted = await postJson(env, `/api/study/sessions/${first.session.id}/submit`, {
      question_id: first.card.question_id,
      version: first.session.version,
      answer: 'Paris',
      idk: false,
    });
    expect(submitted.status).toBe(200);
    const result = (await submitted.json()) as VerdictView;
    expect(result.verdict).toBe('right');
    expect(result.nextDueMinutes).toBe(10);
    expect(result.session).toMatchObject({ id: first.session.id, status: 'active', state: 'showing-result', version: 2 });

    const advanced = await postJson(env, `/api/study/sessions/${first.session.id}/advance`, { version: result.session.version });
    expect(advanced.status).toBe(200);
    const next = (await advanced.json()) as StudyView;
    expect(next.card.question_id).toBe(ids.regex);
    expect(next.card.prompt).toBe('Capital of Peru?');
    expect(next.session).toMatchObject({ id: first.session.id, status: 'active', state: 'awaiting-answer', version: 3 });

    const reviews = storage.rows('reviews');
    expect(reviews).toHaveLength(1);
    expect(reviews[0]?.['question_id']).toBe(ids.mcq);
    expect(reviews[0]?.['result']).toBe('right');
    expect(reviews[0]?.['user_answer']).toBe('Paris');
    expect(cardRow(storage, ids.mcq)['learning_steps']).toBe(1);
  });

  it('rolls back a stale answer before the first Right and graduates on the second', async () => {
    const { env, storage, ids } = await fixture();
    const begun = await postJson(env, '/api/study/decks/offline-e2e/session', {});
    expect(begun.status).toBe(200);
    const first = (await begun.json()) as StudyView;
    const initialCard = cardRow(storage, ids.mcq);
    const initialSession = sessionRow(storage, first.session.id);

    const stale = await postJson(env, `/api/study/sessions/${first.session.id}/submit`, {
      question_id: first.card.question_id,
      version: first.session.version - 1,
      answer: 'Paris',
      idk: false,
    });
    expect(stale.status).toBe(409);
    expect(await stale.json()).toEqual({
      error: {
        code: 'stale_version',
        message: 'session moved on another device',
        current_version: first.session.version,
      },
    });
    expect(storage.rows('reviews')).toHaveLength(0);
    expect(cardRow(storage, ids.mcq)).toEqual(initialCard);
    expect(cardRow(storage, ids.mcq)).toMatchObject({ fsrs_state: 1, learning_steps: 0 });
    expect(sessionRow(storage, first.session.id)).toEqual(initialSession);
    expect(sessionRow(storage, first.session.id)['version']).toBe(first.session.version);

    const retried = await postJson(env, `/api/study/sessions/${first.session.id}/submit`, {
      question_id: first.card.question_id,
      version: first.session.version,
      answer: 'Paris',
      idk: false,
    });
    expect(retried.status).toBe(200);
    const result = (await retried.json()) as VerdictView;
    expect(result.verdict).toBe('right');
    expect(result.nextDueMinutes).toBe(10);
    expect(result.session).toMatchObject({ state: 'showing-result', version: first.session.version + 1 });
    expect(storage.rows('reviews')).toHaveLength(1);
    expect(cardRow(storage, ids.mcq)).toMatchObject({
      fsrs_state: 1,
      learning_steps: 1,
      next_due: '2026-03-14T15:10:00+00:00',
    });

    const graduated = await postJson(
      env,
      '/api/study/decks/offline-e2e/submit',
      { question_id: first.card.question_id, answer: 'Paris', idk: false },
      '2026-03-14T15:10:00Z',
    );
    expect(graduated.status).toBe(200);
    const graduation = (await graduated.json()) as VerdictView;
    expect(graduation.verdict).toBe('right');
    expect(graduation.nextDueMinutes).toBeGreaterThan(24 * 60);
    expect(graduation.session).toBeNull();
    expect(storage.rows('reviews')).toHaveLength(2);
    expect(cardRow(storage, ids.mcq)).toMatchObject({ fsrs_state: 2, learning_steps: 0 });
    expect(sessionRow(storage, first.session.id)['version']).toBe(first.session.version + 1);
  });

  it('rejects a current-version answer for a different question', async () => {
    const { env, storage, ids } = await fixture();
    const begun = (await (await postJson(env, '/api/study/decks/offline-e2e/session', {})).json()) as StudyView;
    const sessionBefore = sessionRow(storage, begun.session.id);
    const currentBefore = cardRow(storage, ids.mcq);
    const otherBefore = cardRow(storage, ids.regex);

    const mismatched = await postJson(env, `/api/study/sessions/${begun.session.id}/submit`, {
      question_id: ids.regex,
      version: begun.session.version,
      answer: 'Lima',
      idk: false,
    });

    expect(mismatched.status).toBe(409);
    expect(await mismatched.json()).toMatchObject({
      error: { code: 'stale_version', current_version: begun.session.version },
    });
    expect(storage.rows('reviews')).toHaveLength(0);
    expect(cardRow(storage, ids.mcq)).toEqual(currentBefore);
    expect(cardRow(storage, ids.regex)).toEqual(otherBefore);
    expect(sessionRow(storage, begun.session.id)).toEqual(sessionBefore);
  });

  it('rejects a stale free-text answer before launching its grading workflow', async () => {
    const { env, storage } = await fixture();
    const begun = (await (await postJson(env, '/api/study/decks/offline-e2e/session', {})).json()) as StudyView;
    const submitted = (await (
      await postJson(env, `/api/study/sessions/${begun.session.id}/submit`, {
        question_id: begun.card.question_id,
        version: begun.session.version,
        answer: 'Paris',
        idk: false,
      })
    ).json()) as VerdictView;
    const next = (await (
      await postJson(env, `/api/study/sessions/${begun.session.id}/advance`, { version: submitted.session.version })
    ).json()) as StudyView;

    const stale = await postJson(env, `/api/study/sessions/${next.session.id}/submit`, {
      question_id: next.card.question_id,
      version: next.session.version - 1,
      answer: 'Lima',
      idk: false,
    });

    expect(stale.status).toBe(409);
    expect(await stale.json()).toMatchObject({ error: { code: 'stale_version', current_version: next.session.version } });
    expect(storage.rows('active_workflows')).toHaveLength(0);
    expect(storage.rows('job_progress')).toHaveLength(0);
    expect(storage.rows('reviews')).toHaveLength(1);
    expect(storage.rows('study_sessions')[0]).toMatchObject({
      id: next.session.id,
      state: 'awaiting-answer',
      version: next.session.version,
    });
  });
});
