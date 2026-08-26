import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import {
  DELETE,
  POLICY,
  REASSIGN,
  REASSIGN_DROP_CONFLICTS,
  applyRule,
  decollideDeckSlugs,
  mergeRows,
  precheck,
  previousUserIds,
  type Row,
  type Snapshot,
} from '../../domain/merge';

const DIR = new URL('../../../tests/fixtures/parity/merge/', import.meta.url).pathname;
const read = (name: string) => JSON.parse(readFileSync(`${DIR}${name}.json`, 'utf8'));

interface Corpus extends Snapshot {
  header: { anon: string; target: string; user_scoped_tables: Record<string, string[]> };
}
interface After extends Corpus {
  account_merges: Row[];
  previous_ids: string[];
  result: { counts: Record<string, number>; merged: boolean; reason: string | null; resolved: boolean };
  target_deck_slugs: string[];
}

const before: Corpus = read('before');
const after: After = read('after');
const { anon, target } = before.header;

const key = (row: Row) => JSON.stringify(row, Object.keys(row).sort());
const multiset = (rows: Row[]) => rows.map(key).sort();

describe('merge oracle', () => {
  const randomHex = vi.fn(() => 'fd58dd');
  const got = mergeRows(before, anon, target, randomHex);

  it('the policy names every user-scoped column of the schema', () => {
    const covered = new Set(POLICY.map((r) => `${r.table}.${r.column}`));
    for (const [table, columns] of Object.entries(before.header.user_scoped_tables)) {
      for (const column of columns) expect(covered.has(`${table}.${column}`), `${table}.${column}`).toBe(true);
    }
  });

  it('draws one random suffix of three bytes', () => {
    expect(randomHex.mock.calls).toEqual([[3]]);
  });

  it('every table holds the rows the reference left, per user', () => {
    let compared = 0;
    for (const [table, columns] of Object.entries(after.tables)) {
      for (const [column, byUser] of Object.entries(columns)) {
        for (const [user, rows] of Object.entries(byUser)) {
          compared++;
          expect(multiset(got.after.tables[table]![column]![user]!), `${table}.${column} ${user}`).toEqual(multiset(rows));
        }
      }
    }
    expect(compared).toBe(22);
  });

  it('the users rows match: target carried, anon gone', () => {
    expect(got.after.users[anon]).toBeNull();
    expect(got.after.users[target]).toEqual(after.users[target]);
  });

  it('counts match the result and the audit row', () => {
    expect(got.counts).toEqual(after.result.counts);
    expect(JSON.parse(String(after.account_merges[0]!['counts']))).toEqual(got.counts);
  });

  it('the target owns the decollided slugs', () => {
    const slugs = got.after.tables['decks']!['user_id']![target]!.map((d) => String(d['name'])).sort();
    expect(slugs).toEqual(after.target_deck_slugs);
  });

  it('previous ids come from completed merges by id', () => {
    expect(previousUserIds(after.account_merges, target)).toEqual(after.previous_ids);
    expect(previousUserIds(after.account_merges, anon)).toEqual([]);
    expect(previousUserIds([{ ...after.account_merges[0], status: 'started' }], target)).toEqual([]);
  });

  it('precheck mirrors the four refusals', () => {
    const anonRow = before.users[anon]!;
    const targetRow = before.users[target]!;
    const refused = (reason: string, resolved: boolean) => ({ resolved, merged: false, counts: {}, reason });
    expect(precheck(anonRow, targetRow, true)).toEqual(refused('same_user', true));
    expect(precheck(null, targetRow, false)).toEqual(refused('anon_missing', true));
    expect(precheck(targetRow, targetRow, false)).toEqual(refused('not_anonymous', false));
    expect(precheck(anonRow, null, false)).toEqual(refused('target_missing', false));
    expect(precheck(anonRow, targetRow, false)).toBeNull();
  });
});

describe('applyRule', () => {
  const rows: Row[] = [
    { user_id: 'a', client_id: 'shared' },
    { user_id: 'a', client_id: 'only-a' },
    { user_id: 't', client_id: 'shared' },
  ];

  it('DELETE removes the anon rows', () => {
    expect(applyRule({ table: 'x', column: 'user_id', rule: DELETE, conflictKey: null }, rows, 'a', 't')).toEqual({
      rows: [rows[2]],
      moved: 2,
      dropped: 0,
    });
  });

  it('REASSIGN rewrites the column', () => {
    const r = applyRule({ table: 'x', column: 'user_id', rule: REASSIGN, conflictKey: null }, rows, 'a', 't');
    expect(r.moved).toBe(2);
    expect(r.rows.map((x) => x['user_id'])).toEqual(['t', 't', 't']);
  });

  it('REASSIGN_DROP_CONFLICTS drops the anon rows the target already has', () => {
    const rule = { table: 'x', column: 'user_id', rule: REASSIGN_DROP_CONFLICTS, conflictKey: 'client_id' } as const;
    expect(applyRule(rule, rows, 'a', 't')).toEqual({
      rows: [
        { user_id: 't', client_id: 'only-a' },
        { user_id: 't', client_id: 'shared' },
      ],
      moved: 1,
      dropped: 1,
    });
  });
});

describe('decollideDeckSlugs', () => {
  const deck = (id: number, name: string): Row => ({ id, name, display_name: name.toUpperCase() });

  it('renames in id order against the union of both namespaces', () => {
    const out = decollideDeckSlugs(
      [deck(9, 'a'), deck(3, 'a-2'), deck(1, 'b')],
      [deck(20, 'a'), deck(21, 'a-3')],
      () => 'zz',
    );
    expect(out.map((d) => d['name'])).toEqual(['a-4', 'a-2', 'b']);
    expect(out[0]!['display_name']).toBe('A');
  });

  it('falls back to random suffixes until one is free', () => {
    const taken = Array.from({ length: 99 }, (_, i) => deck(100 + i, `a-${i + 2}`));
    const draws = ['aaaaaa', 'bbbbbb'];
    const out = decollideDeckSlugs([deck(1, 'a'), deck(2, 'a-aaaaaa')], [deck(50, 'a'), ...taken], () => draws.shift()!);
    expect(out.map((d) => d['name'])).toEqual(['a-bbbbbb', 'a-aaaaaa']);
    expect(draws).toEqual([]);
  });

  it('leaves a clash-free set untouched', () => {
    const anon = [deck(1, 'a')];
    expect(decollideDeckSlugs(anon, [deck(2, 'b')], () => 'x')).toEqual(anon);
  });
});
