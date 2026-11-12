// Activity events: one data point per action, the actor only as a keyed hash,
// and a write that can never fail the request it describes.
import { createHmac } from 'node:crypto';
import { beforeEach, describe, expect, it } from 'vitest';
import { activityEvents, dataPoint } from '../domain/activity';
import { activityLogOf, type ActivityDataset } from '../runtime/adapters/activity';
import { composeWith } from '../runtime/compose';
import { KIND_HEADER, SUBJECT_HEADER, type Route } from '../runtime/cells/router';
import { UserCell } from '../runtime/cells/UserCell';
import type { Env } from '../runtime/env';
import { fakeCellState } from './fakes/sqlStorage';
import { fakeEnv, req, spyRenderer } from './helpers';

const SALT = 'test-salt-not-a-secret';
const USER = 'user_2abc';
const ANON = 'anon:' + 'ab'.repeat(16);
const AT = '2026-03-14T15:00:00+00:00';
const hashOf = (id: string) => createHmac('sha256', SALT).update(`prep:${id}`).digest('hex').slice(0, 16);

type Point = Parameters<ActivityDataset['writeDataPoint']>[0];
function dataset(): ActivityDataset & { points: Point[] } {
  const points: Point[] = [];
  return { points, writeDataPoint: (p) => void points.push(p) };
}

describe('activityEvents', () => {
  it('names only what happened, with its count', () => {
    expect(activityEvents({ active: true, signup: false, reviews: 0, decks: 0 })).toEqual([{ action: 'active', count: 1 }]);
    expect(activityEvents({ active: true, signup: true, reviews: 12, decks: 2 })).toEqual([
      { action: 'active', count: 1 },
      { action: 'signup', count: 1 },
      { action: 'card_review', count: 12 },
      { action: 'deck_created', count: 2 },
    ]);
    expect(activityEvents({ active: false, signup: false, reviews: -3, decks: 0 })).toEqual([]);
  });

  it('lays a point out as one index, three blobs and one double', () => {
    expect(dataPoint('abcd', { action: 'card_review', count: 12 })).toEqual({ indexes: ['abcd'], blobs: ['prep', 'card_review', ''], doubles: [12] });
  });
});

describe('the activity log', () => {
  it('writes the actor as the first 16 hex of a keyed hash, never the id', async () => {
    const ds = dataset();
    await activityLogOf(ds, SALT).record(USER, [{ action: 'active', count: 1 }]);
    expect(ds.points).toEqual([{ indexes: [hashOf(USER)], blobs: ['prep', 'active', ''], doubles: [1] }]);
    expect(JSON.stringify(ds.points)).not.toContain(USER);
  });

  it('writes nothing without a dataset or without a salt', async () => {
    const ds = dataset();
    await activityLogOf(undefined, SALT).record(USER, [{ action: 'active', count: 1 }]);
    await activityLogOf(ds, undefined).record(USER, [{ action: 'active', count: 1 }]);
    await activityLogOf(ds, '  ').record(USER, [{ action: 'active', count: 1 }]);
    expect(ds.points).toEqual([]);
  });

  it('never rejects when the dataset throws', async () => {
    const log = activityLogOf({ writeDataPoint: () => { throw new Error('sink down'); } }, SALT);
    await expect(log.record(USER, [{ action: 'active', count: 1 }])).resolves.toBeUndefined();
  });
});

describe('the user cell reports activity', () => {
  let env: Env;
  let ds: ReturnType<typeof dataset>;
  let cell: UserCell;

  class RoutedCell extends UserCell {
    constructor(ctx: DurableObjectState, cellEnv: Env, private readonly extra: readonly Route[]) {
      super(ctx, cellEnv);
    }
    protected override routes(): readonly Route[] {
      return [...this.extra, ...super.routes()];
    }
  }

  const ROUTES: Route[] = [
    { method: 'GET', pattern: '/t/read', gate: 'user', handler: () => ({ json: {} }) },
    { method: 'GET', pattern: '/api/active-workflows-badge', gate: 'user', handler: () => ({ json: {} }) },
    {
      method: 'POST',
      pattern: '/t/deck',
      gate: 'user',
      handler: ({ repos }) => {
        const deck = repos.decks.create('capitals');
        repos.decks.create('rivers');
        const qid = repos.questions.add(deck, { type: 'short', prompt: 'Capital of France?', answer: 'Paris' } as never);
        return { json: { qid } };
      },
    },
    {
      method: 'POST',
      pattern: '/t/review/{qid:int}',
      gate: 'user',
      handler: ({ repos, params }) => {
        for (let i = 0; i < 3; i++) repos.reviews.record(Number(params['qid']), 'right', 'Paris');
        return { json: {} };
      },
    },
    { method: 'POST', pattern: '/t/boom', gate: 'user', handler: () => { throw new Error('handler exploded'); } },
  ];

  beforeEach(async () => {
    ds = dataset();
    env = fakeEnv({ ACTIVITY: ds, ACTIVITY_SALT: SALT });
    composeWith(env, { renderer: spyRenderer() });
    const state = fakeCellState();
    cell = new RoutedCell(state, env, ROUTES);
    await state.ready();
  });

  const as = (subject: string, path: string, init: RequestInit = {}, kind?: string) =>
    cell.fetch(req(path, { ...init, headers: { [SUBJECT_HEADER]: subject, ...(kind ? { [KIND_HEADER]: kind } : {}) } }));
  const actions = () => ds.points.map((p) => [p.blobs[1], p.doubles[0]]);

  it('a new user’s first request is active and a signup; the next is only active', async () => {
    expect((await as(USER, '/t/read')).status).toBe(200);
    expect(actions()).toEqual([['active', 1], ['signup', 1]]);
    ds.points.length = 0;
    await as(USER, '/t/read');
    expect(actions()).toEqual([['active', 1]]);
    expect(ds.points[0]!.indexes).toEqual([hashOf(USER)]);
  });

  it('counts the decks and the reviews a request wrote', async () => {
    await as(USER, '/t/read');
    ds.points.length = 0;
    const { qid } = (await (await as(USER, '/t/deck', { method: 'POST' })).json()) as { qid: number };
    expect(actions()).toEqual([['active', 1], ['deck_created', 2]]);
    ds.points.length = 0;
    await as(USER, `/t/review/${qid}`, { method: 'POST' });
    expect(actions()).toEqual([['active', 1], ['card_review', 3]]);
  });

  it('a background poll is not activity', async () => {
    await as(USER, '/t/read');
    ds.points.length = 0;
    expect((await as(USER, '/api/active-workflows-badge')).status).toBe(200);
    expect(actions()).toEqual([]);
  });

  it('a request that asks to be skipped writes nothing, a new user’s first included', async () => {
    const skipped = (subject: string, path: string, init: RequestInit = {}) =>
      cell.fetch(req(path, { ...init, headers: { [SUBJECT_HEADER]: subject, 'X-Activity-Skip': '1' } }));
    expect((await skipped(USER, '/t/read')).status).toBe(200);
    expect((await skipped(USER, '/t/deck', { method: 'POST' })).status).toBe(200);
    expect(ds.points).toEqual([]);
    await as(USER, '/t/read');
    expect(actions()).toEqual([['active', 1]]);
  });

  it('an anonymous account writes nothing', async () => {
    await cell.createInstantDeck({ displayName: 'Guest deck', cards: [], mint: { id: ANON, displayName: 'Guest', idx: 7 }, at: AT });
    expect((await as(ANON, '/t/read', {}, 'anon')).status).toBe(200);
    expect(ds.points).toEqual([]);
  });

  it('an instant deck for a signed-in user is a deck created', async () => {
    await as(USER, '/t/read');
    ds.points.length = 0;
    await cell.createInstantDeck({ displayName: 'Rivers', cards: [], mint: null, at: AT });
    expect(actions()).toEqual([['deck_created', 1]]);
  });

  it('an account first written outside a request is a signup, once', async () => {
    await cell.upsert(USER, { displayName: 'Ada' }, AT, 3);
    await cell.upsert(USER, { displayName: 'Ada L' }, AT, 3);
    expect(actions()).toEqual([['signup', 1]]);
    ds.points.length = 0;
    await as(USER, '/t/read');
    expect(actions()).toEqual([['active', 1]]);
  });

  it('a failing sink or a throwing handler changes no response', async () => {
    ds.writeDataPoint = () => { throw new Error('sink down'); };
    expect((await as(USER, '/t/read')).status).toBe(200);
    await expect(as(USER, '/t/boom', { method: 'POST' })).rejects.toThrow('handler exploded');
  });

  it('a cell with no binding serves as before', async () => {
    const bare = fakeEnv();
    composeWith(bare, { renderer: spyRenderer() });
    const state = fakeCellState();
    const plain = new RoutedCell(state, bare, ROUTES);
    await state.ready();
    expect((await plain.fetch(req('/t/read', { headers: { [SUBJECT_HEADER]: USER } }))).status).toBe(200);
  });
});
