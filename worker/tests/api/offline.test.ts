import { beforeAll, describe, expect, it } from 'vitest';
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import type { FakeCellStorage } from '../fakes/sqlStorage.js';
import { INTERNAL_TOKEN, ORIGIN, SEED_USER, seed, workerEnv } from './harness.js';

let env: Env;
let storage: FakeCellStorage;

const authHeaders = {
  accept: 'application/json',
  'tailscale-user-login': SEED_USER,
  'tailscale-user-name': 'Seed',
  'x-internal-token': INTERNAL_TOKEN,
};

function request(path: string, init: RequestInit = {}, authenticated = true): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set('accept', 'application/json');
  if (authenticated) {
    for (const [name, value] of Object.entries(authHeaders)) headers.set(name, value);
  }
  return worker.fetch(new Request(`${ORIGIN}${path}`, { ...init, headers }), env);
}

function postSync(body: unknown, authenticated = true): Promise<Response> {
  return request(
    '/api/offline/sync',
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: typeof body === 'string' ? body : JSON.stringify(body),
    },
    authenticated,
  );
}

function rowCounts(): Record<string, number> {
  return Object.fromEntries(['decks', 'questions', 'cards', 'reviews', 'offline_sync_idempotency'].map((table) => [table, storage.rows(table).length]));
}

beforeAll(async () => {
  const state = workerEnv();
  env = state.env;
  storage = state.userStorage(SEED_USER);
  await seed(env, 'reader', SEED_USER);
}, 60_000);

describe('the offline HTTP routes', () => {
  it('returns an authenticated snapshot and refuses an unauthenticated read', async () => {
    const found = await request('/api/offline/snapshot');
    expect(found.status).toBe(200);
    const body = (await found.json()) as {
      user: { id: string; display_name: string; previous_ids: string[] };
      generated_at: string;
      decks: Array<Record<string, unknown>>;
      cards: Array<Record<string, unknown>>;
    };
    expect(body.user).toEqual({ id: SEED_USER, display_name: 'Seed', previous_ids: [] });
    expect(body.generated_at).toBe('2026-03-14T15:00:00+00:00');
    expect(body.decks.find((deck) => deck['name'] === 'world-capitals')).toMatchObject({
      display_name: 'World Capitals',
      total: 6,
    });
    expect(body.cards.find((card) => card['prompt'] === 'Which city is the capital of Australia?')).toMatchObject({
      type: 'mcq',
      answer: 'Canberra',
      choices: ['Sydney', 'Canberra', 'Melbourne', 'Perth'],
      next_due: '2026-03-14T12:00:00+00:00',
    });

    const refused = await request('/api/offline/snapshot', {}, false);
    expect(refused.status).toBe(401);
    expect(await refused.json()).toEqual({ detail: 'not authenticated' });
  });

  it('syncs one card and review idempotently, then exposes their state', async () => {
    const batch = {
      new_cards: [
        {
          client_id: 'offline-card-http-1',
          deck_name: 'Offline Notes',
          prompt: 'Which protocol reconciles this batch?',
          answer: 'The offline sync protocol',
        },
      ],
      reviews: [
        {
          client_id: 'offline-review-http-1',
          card_client_id: 'offline-card-http-1',
          verdict: 'right',
          graded_by: 'self',
          user_answer: 'The offline sync protocol',
          reviewed_at: '2026-03-14T15:00:00Z',
        },
      ],
    };
    const before = rowCounts();

    const first = await postSync(batch);
    expect(first.status).toBe(200);
    const firstBody = (await first.json()) as {
      cards: Array<{ client_id: string; status: string; question_id: number }>;
      reviews: Array<{ client_id: string; status: string }>;
    };
    expect(firstBody.cards).toEqual([
      { client_id: 'offline-card-http-1', status: 'created', question_id: expect.any(Number) },
    ]);
    expect(firstBody.reviews).toEqual([{ client_id: 'offline-review-http-1', status: 'applied' }]);

    const afterFirst = rowCounts();
    expect(afterFirst).toEqual({
      decks: before['decks']! + 1,
      questions: before['questions']! + 1,
      cards: before['cards']! + 1,
      reviews: before['reviews']! + 1,
      offline_sync_idempotency: before['offline_sync_idempotency']! + 2,
    });

    const retry = await postSync(batch);
    expect(retry.status).toBe(200);
    expect(await retry.json()).toEqual(firstBody);
    expect(rowCounts()).toEqual(afterFirst);

    const snapshot = await request('/api/offline/snapshot');
    expect(snapshot.status).toBe(200);
    const snapshotBody = (await snapshot.json()) as {
      decks: Array<{ id: number; name: string; display_name: string | null }>;
      cards: Array<Record<string, unknown>>;
    };
    const deck = snapshotBody.decks.find((candidate) => candidate.name === 'offline-notes');
    expect(deck).toMatchObject({ display_name: 'Offline Notes' });
    const card = snapshotBody.cards.find((candidate) => candidate['question_id'] === firstBody.cards[0]!.question_id);
    expect(card).toMatchObject({
      deck_id: deck!.id,
      type: 'short',
      prompt: 'Which protocol reconciles this batch?',
      answer: 'The offline sync protocol',
      answer_regex: null,
      step: expect.any(Number),
      next_due: expect.any(String),
    });
    expect(Date.parse(card!['next_due'] as string)).toBeGreaterThan(Date.parse('2026-03-14T15:00:00Z'));
  });

  it('rejects malformed input and an unauthenticated sync', async () => {
    const malformedJson = await postSync('{');
    expect(malformedJson.status).toBe(422);
    const jsonError = (await malformedJson.json()) as { detail: Array<Record<string, unknown>> };
    expect(jsonError.detail).toHaveLength(1);
    expect(jsonError.detail[0]).toMatchObject({ type: 'json_invalid', loc: ['body', 1] });

    const malformedItem = await postSync({ new_cards: ['not an object'], reviews: [] });
    expect(malformedItem.status).toBe(422);
    const itemError = (await malformedItem.json()) as { detail: Array<Record<string, unknown>> };
    expect(itemError.detail).toHaveLength(1);
    expect(itemError.detail[0]).toMatchObject({ type: 'model_attributes_type', loc: ['body', 'new_cards', 0] });

    const refused = await postSync({ new_cards: [], reviews: [] }, false);
    expect(refused.status).toBe(401);
    expect(await refused.json()).toEqual({ detail: 'not authenticated' });
  });
});
