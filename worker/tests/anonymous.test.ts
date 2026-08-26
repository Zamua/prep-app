import { describe, expect, it } from 'vitest';
import { GONE_MERGING, GONE_MISSING, anonAccess, cookieVerdict } from '../app/auth/anonymous.js';
import { destroyAccount } from '../app/auth/mergeSaga.js';
import type { Clock, Precheck } from '../app/ports.js';
import type { MergeMarker, TombstoneReason } from '../app/entities.js';
import { SUBJECT_HEADER, KIND_HEADER } from '../runtime/cells/router.js';
import { TOMBSTONED_HEADER, UserCell } from '../runtime/cells/UserCell.js';
import { FakeDirectory, FakeUserCells } from './fakes/cells.js';
import { fakeCellState } from './fakes/sqlStorage.js';
import { fakeEnv, req } from './helpers.js';

const ANON = 'anon:' + 'ab'.repeat(16);
const AT = '2026-03-14T15:00:00+00:00';

const live: Precheck = { exists: true, isAnonymous: true, tombstoned: null };
const marker: MergeMarker = { anon_id: ANON, target_id: 'parity@example.com', audit_id: 1, started_at: AT };

describe('anonAccess', () => {
  it('serves a live anonymous account', () => {
    expect(anonAccess(live)).toEqual({ kind: 'serve' });
    expect(anonAccess(live, null)).toEqual({ kind: 'serve' });
  });

  it.each(['merged', 'reaped', 'deleted'] as TombstoneReason[])('reports a %s cell gone, with its reason', (reason) => {
    expect(anonAccess({ exists: false, isAnonymous: false, tombstoned: reason })).toEqual({ kind: 'gone', reason });
  });

  it('reports an id with no cell gone', () => {
    expect(anonAccess({ exists: false, isAnonymous: false, tombstoned: null })).toEqual({ kind: 'gone', reason: GONE_MISSING });
  });

  it('reports an id whose rows are moving gone, before its cell knows', () => {
    expect(anonAccess(live, marker)).toEqual({ kind: 'gone', reason: GONE_MERGING });
  });

  it('lets the tombstone outrank the marker: the rows have already left', () => {
    expect(anonAccess({ exists: false, isAnonymous: false, tombstoned: 'merged' }, marker)).toEqual({ kind: 'gone', reason: 'merged' });
  });
});

describe('cookieVerdict', () => {
  it('clears only what the merge resolved', () => {
    expect(cookieVerdict({ resolved: true, merged: true, counts: {}, reason: null })).toBe('clear');
    expect(cookieVerdict({ resolved: true, merged: false, counts: {}, reason: 'anon_missing' })).toBe('clear');
    expect(cookieVerdict({ resolved: false, merged: false, counts: {}, reason: 'not_anonymous' })).toBe('keep');
  });
});

describe('a destroyed cell answers for itself', () => {
  it('refuses every request with its reason, across a reactivation', async () => {
    const env = fakeEnv();
    const cells = new FakeUserCells(env);
    const directory = new FakeDirectory();
    const clock: Clock = { now: () => new Date(AT) };
    await directory.register(ANON, true, AT);
    await cells.cell(ANON).createInstantDeck({
      displayName: 'Capitals',
      cards: [{ prompt: 'Capital of France?', answer: 'Paris', answer_regex: null }],
      mint: { id: ANON, displayName: 'Guest', idx: 7 },
      at: AT,
    });

    await destroyAccount(ANON, 'deleted', { cells, directory, clock });

    const { storage } = cells.entry(ANON);
    const revived = new UserCell(fakeCellState(storage), env);
    const res = await revived.fetch(req('/', { headers: { [SUBJECT_HEADER]: ANON, [KIND_HEADER]: 'anon' } }));
    expect(res.status).toBe(410);
    expect(res.headers.get(TOMBSTONED_HEADER)).toBe('deleted');
    expect(await res.json()).toEqual({ tombstoned: 'deleted' });
    expect(anonAccess(await revived.precheck())).toEqual({ kind: 'gone', reason: 'deleted' });
    // Re-migrating an emptied cell must not look like a fresh account.
    expect(storage.rows('profile')).toEqual([]);
  });
});
