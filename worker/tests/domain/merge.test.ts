import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import {
  DELETE,
  POLICY,
  REASSIGN,
  REASSIGN_DROP_CONFLICTS,
  applyRule,
  decollideDeckSlugs,
  type Row,
} from '../../domain/merge';

const DIR = new URL('../../../tests/fixtures/merge/', import.meta.url).pathname;

describe('the policy', () => {
  // A user-scoped column the policy does not name is a table the merge
  // leaves behind, which reads as data loss to whoever signed in.
  it('names every user-scoped column the scenario carries', () => {
    const header = (JSON.parse(readFileSync(`${DIR}before.json`, 'utf8')) as {
      header: { user_scoped_tables: Record<string, string[]> };
    }).header;
    const covered = new Set(POLICY.map((r) => `${r.table}.${r.column}`));
    for (const [table, columns] of Object.entries(header.user_scoped_tables)) {
      for (const column of columns) expect(covered.has(`${table}.${column}`), `${table}.${column}`).toBe(true);
    }
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
