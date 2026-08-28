import { beforeEach, describe, expect, it } from 'vitest';
import { DirectoryCell } from '../runtime/cells/DirectoryCell.js';
import { FakeDirectory } from './fakes/cells.js';
import { fakeCellState } from './fakes/sqlStorage.js';
import { fakeEnv } from './helpers.js';

const T0 = '2026-03-14T15:00:00+00:00';
const T1 = '2026-03-14T15:00:01+00:00';
const ANON = 'anon:' + 'ab'.repeat(16);
const TARGET = 'seed@example.com';

describe.each([
  ['DirectoryCell', () => new DirectoryCell(fakeCellState(), fakeEnv())],
  ['FakeDirectory', () => new FakeDirectory()],
])('%s', (_name, make) => {
  let dir: ReturnType<typeof make>;
  beforeEach(() => {
    dir = make();
  });

  it('registers with a fresh idx, idempotently, and pins idx 0 for the seed', async () => {
    expect(await dir.register(TARGET, false, T0, { idx: 0 })).toEqual({ idx: 0 });
    expect(await dir.register(TARGET, false, T1)).toEqual({ idx: 0 });
    expect(await dir.register('a', false, T0)).toEqual({ idx: 1 });
    expect(await dir.register(ANON, true, T0)).toEqual({ idx: 2 });
    expect(await dir.lookup(ANON)).toEqual({ id: ANON, is_anonymous: true, created_at: T0, idx: 2 });
    expect(await dir.lookup('nope')).toBeNull();
  });

  it('a merge is a marker plus a started audit row; completion records counts, failure clears the marker', async () => {
    await dir.register(ANON, true, T0);
    await dir.register(TARGET, false, T0);
    const begun = await dir.beginMerge(ANON, TARGET, T0);
    expect(begun.marker).toEqual({ anon_id: ANON, target_id: TARGET, audit_id: begun.auditId, started_at: T0 });
    expect(await dir.beginMerge(ANON, TARGET, T1)).toEqual(begun);
    expect(await dir.marker(ANON)).toEqual(begun.marker);
    expect((await dir.audit(begun.auditId))?.status).toBe('started');
    expect(await dir.previousIds(TARGET)).toEqual([]);
    await dir.completeMerge(begun.auditId, { decks: 2, questions: 5 }, T1);
    expect(await dir.audit(begun.auditId)).toMatchObject({ status: 'completed', completed_at: T1, counts: { decks: 2, questions: 5 } });
    expect(await dir.previousIds(TARGET)).toEqual([ANON]);
    await dir.clearMarker(ANON);
    expect(await dir.marker(ANON)).toBeNull();

    const second = await dir.beginMerge('anon:' + 'cd'.repeat(16), TARGET, T1);
    await dir.failMerge(second.auditId, 'boom', T1);
    expect(await dir.audit(second.auditId)).toMatchObject({ status: 'failed', error: 'boom' });
    expect(await dir.marker('anon:' + 'cd'.repeat(16))).toBeNull();
    expect(await dir.previousIds(TARGET)).toEqual([ANON]);
  });

  it('tombstones, removes, and walks anonymous ids in pages', async () => {
    for (const id of ['anon:c', 'anon:a', 'anon:b']) await dir.register(id, true, T0);
    await dir.register(TARGET, false, T0);
    expect((await dir.listAnonymous(null, 2)).map((u) => u.id)).toEqual(['anon:a', 'anon:b']);
    expect((await dir.listAnonymous('anon:b', 2)).map((u) => u.id)).toEqual(['anon:c']);
    await dir.tombstone('anon:a', 'reaped', T1);
    await dir.tombstone('anon:a', 'merged', T1);
    expect(await dir.tombstoneOf('anon:a')).toEqual({ reason: 'reaped', at: T1 });
    expect(await dir.tombstoneOf('anon:b')).toBeNull();
    await dir.remove('anon:a');
    expect(await dir.lookup('anon:a')).toBeNull();
    expect((await dir.listAnonymous(null, 10)).map((u) => u.id)).toEqual(['anon:b', 'anon:c']);
  });
});

describe('DirectoryCell', () => {
  it('answers rpc only over fetch', async () => {
    const res = await new DirectoryCell(fakeCellState(), fakeEnv()).fetch(new Request('https://x/'));
    expect(res.status).toBe(501);
  });
});
