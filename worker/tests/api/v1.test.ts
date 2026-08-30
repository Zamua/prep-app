import { beforeAll, describe, expect, it } from 'vitest';
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import { mintToken, ORIGIN, SEED_USER, seed, workerEnv } from './harness.js';

let env: Env;
let bearer: string;

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set('authorization', `Bearer ${bearer}`);
  return worker.fetch(new Request(`${ORIGIN}${path}`, { ...init, headers }), env);
}

async function postJson(path: string, body: unknown): Promise<Response> {
  return request(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

beforeAll(async () => {
  const state = workerEnv();
  env = state.env;
  await seed(env, 'reader', SEED_USER);
  bearer = await mintToken(state.userStorage(SEED_USER), SEED_USER, 'v1');
}, 60_000);

describe('the V1 deck routes', () => {
  it('lists deck summaries', async () => {
    const res = await request('/api/v1/decks');
    expect(res.status).toBe(200);

    const body = (await res.json()) as { decks: Array<Record<string, unknown>> };
    expect(body.decks.find((deck) => deck['name'] === 'world-capitals')).toMatchObject({
      name: 'world-capitals',
      type: 'srs',
      card_count: 6,
      pinned: false,
    });
    expect(body.decks.find((deck) => deck['name'] === 'world-history')).toMatchObject({
      type: 'trivia',
      card_count: 3,
    });
  });

  it('creates a deck and reports duplicate and invalid names', async () => {
    const created = await postJson('/api/v1/decks', { name: 'api-created', context_prompt: 'HTTP API coverage' });
    expect(created.status).toBe(200);
    expect(await created.json()).toMatchObject({ name: 'api-created', id: expect.any(Number) });

    const duplicate = await postJson('/api/v1/decks', { name: 'api-created' });
    expect(duplicate.status).toBe(409);
    expect(await duplicate.json()).toEqual({ detail: "deck 'api-created' already exists" });

    const invalid = await postJson('/api/v1/decks', { name: 'x' });
    expect(invalid.status).toBe(422);
    const body = (await invalid.json()) as { detail: Array<Record<string, unknown>> };
    expect(body.detail).toHaveLength(1);
    expect(body.detail[0]).toMatchObject({
      type: 'string_too_short',
      loc: ['body', 'name'],
      input: 'x',
      ctx: { min_length: 2 },
    });
  });

  it('returns metadata and a not-found detail', async () => {
    const found = await request('/api/v1/decks/world-capitals');
    expect(found.status).toBe(200);
    expect(await found.json()).toEqual({
      name: 'world-capitals',
      type: 'srs',
      context_prompt: 'Capital cities of the world, one card per country.',
      card_count: 6,
    });

    const missing = await request('/api/v1/decks/missing-deck');
    expect(missing.status).toBe(404);
    expect(await missing.json()).toEqual({ detail: 'deck not found' });
  });

  it('lists cards and reports a missing deck', async () => {
    const found = await request('/api/v1/decks/world-capitals/cards');
    expect(found.status).toBe(200);
    const body = (await found.json()) as { deck: string; cards: Array<Record<string, unknown>> };
    expect(body.deck).toBe('world-capitals');
    expect(body.cards).toHaveLength(6);
    expect(body.cards[0]).toMatchObject({
      type: 'mcq',
      topic: 'oceania',
      prompt: 'Which city is the capital of Australia?',
      answer: 'Canberra',
      choices: ['Sydney', 'Canberra', 'Melbourne', 'Perth'],
    });

    const missing = await request('/api/v1/decks/missing-deck/cards');
    expect(missing.status).toBe(404);
    expect(await missing.json()).toEqual({ detail: 'deck not found' });
  });

  it('exports CSV with download headers and reports a missing deck', async () => {
    const found = await request('/api/v1/decks/world-capitals/export.csv');
    expect(found.status).toBe(200);
    expect(found.headers.get('content-type')).toBe('text/csv; charset=utf-8');
    expect(found.headers.get('content-disposition')).toBe('attachment; filename="world-capitals.csv"');
    const csv = await found.text();
    expect(csv).toMatch(/^type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation\r\n/);
    expect(csv).toContain('Which city is the capital of Australia?');

    const missing = await request('/api/v1/decks/missing-deck/export.csv');
    expect(missing.status).toBe(404);
    expect(await missing.json()).toEqual({ detail: 'deck not found' });
  });

  it('imports valid, duplicate, and invalid CSV rows and rejects an empty body', async () => {
    const csv = [
      'type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation',
      'short,api,Fresh question?,Fresh answer,,,,,,',
      'short,api,Fresh question?,Fresh answer,,,,,,',
      'essay,api,Unsupported question?,Nope,,,,,,',
    ].join('\n');
    const imported = await request('/api/v1/decks/http-import/import-csv', {
      method: 'POST',
      headers: { 'content-type': 'text/csv' },
      body: csv,
    });
    expect(imported.status).toBe(200);
    const outcome = (await imported.json()) as {
      deck_id: number;
      deck_name: string;
      inserted: number;
      skipped_duplicates: number;
      errors: string[];
    };
    expect(outcome.deck_id).toEqual(expect.any(Number));
    expect(outcome.deck_name).toBe('http-import');
    expect(outcome.inserted).toBe(1);
    expect(outcome.skipped_duplicates).toBe(1);
    expect(outcome.errors).toEqual(["row 4: unknown type 'essay'"]);

    const empty = await request('/api/v1/decks/empty-import/import-csv', { method: 'POST', body: '  \n' });
    expect(empty.status).toBe(400);
    expect(await empty.json()).toEqual({ detail: 'empty CSV body' });
  });
});
