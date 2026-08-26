// Merging an anonymous account into a provider account: the policy as
// data and the row mapping it implies. Transactions, audit rows, schema
// discovery and the leftover assertion belong to the cell.

export type Row = Record<string, unknown>;

export const REASSIGN = 'reassign';
export const REASSIGN_DROP_CONFLICTS = 'reassign_drop_conflicts';
export const DELETE = 'delete';
export type RuleKind = typeof REASSIGN | typeof REASSIGN_DROP_CONFLICTS | typeof DELETE;

export interface TableRule {
  readonly table: string;
  readonly column: string;
  readonly rule: RuleKind;
  /** Non-user half of the primary key, for the drop-conflicts rule. */
  readonly conflictKey: string | null;
}

const rule = (table: string, column: string, kind: RuleKind, conflictKey: string | null = null): TableRule => ({
  table,
  column,
  rule: kind,
  conflictKey,
});

// Order matters: parents before children.
export const POLICY: readonly TableRule[] = [
  rule('decks', 'user_id', REASSIGN),
  rule('questions', 'user_id', REASSIGN),
  rule('study_sessions', 'user_id', REASSIGN),
  rule('trivia_sessions', 'user_id', REASSIGN),
  rule('notifications_log', 'user_id', REASSIGN),
  rule('active_workflows', 'user_login', REASSIGN),
  rule('offline_sync_idempotency', 'user_id', REASSIGN_DROP_CONFLICTS, 'client_id'),
  rule('instant_generations', 'user_id', REASSIGN),
  rule('push_subscriptions', 'user_id', DELETE),
  rule('byok_credentials', 'user_id', DELETE),
  rule('api_tokens', 'user_id', DELETE),
];

/**
 * Row identity per reassigned table: what a resumed merge uses to recognise
 * the rows an earlier attempt already moved, so the slug de-collision sees
 * the same input twice and never chases a suffix it minted itself. Only
 * tables whose ids come from a cell's id block appear; the client-keyed
 * ledgers share a key space between accounts, so a matching key is no
 * evidence the row came from the anonymous cell, and a table absent here
 * keeps every row.
 */
export const ROW_KEYS: Readonly<Record<string, readonly string[]>> = {
  decks: ['id'],
  questions: ['id'],
  study_sessions: ['id'],
  trivia_sessions: ['id'],
  notifications_log: ['id'],
  active_workflows: ['workflow_id'],
};

/**
 * All the policy reads from the target: the slug namespace it de-collides
 * against, and the client keys the drop-conflicts rule tests. Every other
 * table contributes nothing but rows that are already the target's, so a
 * whole-cell dump would make each attempt cost a full read of the account
 * that is not moving.
 */
export const TARGET_COLUMNS: Readonly<Record<string, readonly string[]>> = {
  decks: ['id', 'name'],
  offline_sync_idempotency: ['client_id'],
};

/** The row's identity within `table`, or null when the table has none. */
export function rowKey(table: string, row: Row): string | null {
  const columns = ROW_KEYS[table];
  if (!columns) return null;
  return JSON.stringify(columns.map((c) => row[c] ?? null));
}

// The two `users` columns an anonymous user may have set; COPY-IF-NULL
// onto the target. `desired_retention` already shaped the merged cards'
// `next_due`, so it travels with them.
export const CARRIED_USER_COLUMNS = ['desired_retention', 'editor_input_mode'] as const;

// Columns that die with the anonymous row. With CARRIED_USER_COLUMNS this
// enumerates every `users` column, which the schema-drift check asserts.
export const DROPPED_USER_COLUMNS = [
  'tailscale_login',
  'display_name',
  'email',
  'profile_pic_url',
  'created_at',
  'last_seen_at',
  'is_anonymous',
  'notification_prefs',
  'active_byok_provider',
] as const;

// Slug de-collision: numbered suffixes first, then random ones without
// bound, so a user with a hundred same-named decks stays mergeable.
export const NUMBERED_SUFFIXES: readonly [number, number] = [2, 100];
export const SUFFIX_BYTES = 3;

export type Counts = Record<string, number>;

/** `resolved`: the cookie is safe to discard. `merged`: data moved. */
export interface MergeResult {
  resolved: boolean;
  merged: boolean;
  counts: Counts;
  reason: string | null;
}

/** Rows per table, per owner column, per user id; `users` rows by id. */
export interface Snapshot {
  users: Record<string, Row | null>;
  tables: Record<string, Record<string, Record<string, Row[]>>>;
}

export type RandomHex = (bytes: number) => string;

export class MissingUserRow extends Error {}

export function applyRule(
  rule: TableRule,
  rows: readonly Row[],
  anon: string,
  target: string,
): { rows: Row[]; moved: number; dropped: number } {
  const { column } = rule;
  if (rule.rule === DELETE) {
    const kept = rows.filter((r) => r[column] !== anon);
    return { rows: kept, moved: rows.length - kept.length, dropped: 0 };
  }
  let live: readonly Row[] = rows;
  let dropped = 0;
  if (rule.rule === REASSIGN_DROP_CONFLICTS) {
    const key = rule.conflictKey ?? '';
    const taken = new Set(rows.filter((r) => r[column] === target).map((r) => r[key]));
    live = rows.filter((r) => !(r[column] === anon && taken.has(r[key])));
    dropped = rows.length - live.length;
  }
  let moved = 0;
  const out = live.map((r) => {
    if (r[column] !== anon) return r;
    moved++;
    return { ...r, [column]: target };
  });
  return { rows: out, moved, dropped };
}

/** COPY-IF-NULL onto the target; a target who already chose keeps it. */
export function carryPreferences(anon: Row, target: Row): { row: Row; counts: Counts } {
  const row: Row = { ...target };
  const counts: Counts = {};
  for (const col of CARRIED_USER_COLUMNS) {
    if ((target[col] ?? null) === null && (anon[col] ?? null) !== null) {
      row[col] = anon[col];
      counts[`users.${col}`] = 1;
    }
  }
  return { row, counts };
}

const byId = (a: Row, b: Row) => Number(a['id']) - Number(b['id']);

function freeSlug(name: string, taken: ReadonlySet<string>, randomHex: RandomHex): string {
  const [first, last] = NUMBERED_SUFFIXES;
  for (let n = first; n <= last; n++) {
    const candidate = `${name}-${n}`;
    if (!taken.has(candidate)) return candidate;
  }
  for (;;) {
    const candidate = `${name}-${randomHex(SUFFIX_BYTES)}`;
    if (!taken.has(candidate)) return candidate;
  }
}

/**
 * Renames anonymous decks whose `name` the target already uses, in `id`
 * order, against the union of both namespaces. `display_name` is kept:
 * only the URL slug moves.
 */
export function decollideDeckSlugs(anonDecks: readonly Row[], targetDecks: readonly Row[], randomHex: RandomHex): Row[] {
  const targetNames = new Set(targetDecks.map((d) => String(d['name'])));
  const clashes = anonDecks.filter((d) => targetNames.has(String(d['name']))).sort(byId);
  if (clashes.length === 0) return [...anonDecks];
  const taken = new Set([...anonDecks, ...targetDecks].map((d) => String(d['name'])));
  const renamed = new Map<unknown, string>();
  for (const deck of clashes) {
    const name = freeSlug(String(deck['name']), taken, randomHex);
    taken.add(name);
    renamed.set(deck['id'], name);
  }
  return anonDecks.map((d) => (renamed.has(d['id']) ? { ...d, name: renamed.get(d['id']) } : d));
}

/** Every rule in policy order, then the preference carry; counts omit zeros. */
export function mergeRows(
  before: Snapshot,
  anon: string,
  target: string,
  randomHex: RandomHex,
): { after: Snapshot; counts: Counts } {
  const anonRow = before.users[anon];
  const targetRow = before.users[target];
  if (!anonRow || !targetRow) throw new MissingUserRow(anonRow ? 'target' : 'anon');
  const tables = { ...before.tables };
  const counts: Counts = {};

  const decks = tables['decks']?.['user_id'];
  if (decks) {
    const renamed = decollideDeckSlugs(decks[anon] ?? [], decks[target] ?? [], randomHex);
    tables['decks'] = { ...tables['decks'], user_id: { ...decks, [anon]: renamed } };
  }

  for (const rule of POLICY) {
    const byUser = tables[rule.table]?.[rule.column];
    if (!byUser) continue;
    const rows = [...(byUser[anon] ?? []), ...(byUser[target] ?? [])];
    const applied = applyRule(rule, rows, anon, target);
    if (applied.dropped) counts[`${rule.table}.dropped`] = applied.dropped;
    if (applied.moved) counts[rule.table] = applied.moved;
    tables[rule.table] = {
      ...tables[rule.table],
      [rule.column]: {
        ...byUser,
        [anon]: applied.rows.filter((r) => r[rule.column] === anon),
        [target]: applied.rows.filter((r) => r[rule.column] === target),
      },
    };
  }

  const carried = carryPreferences(anonRow, targetRow);
  Object.assign(counts, carried.counts);
  const users = { ...before.users, [anon]: null, [target]: carried.row };
  return { after: { users, tables }, counts };
}

const refuse = (reason: string, resolved: boolean): MergeResult => ({ resolved, merged: false, counts: {}, reason });

export const SAME_USER = 'same_user';
/** The cookie names an account that is gone: reaped, deleted, or merged elsewhere. */
export const ANON_MISSING = 'anon_missing';
export const NOT_ANONYMOUS = 'not_anonymous';
export const TARGET_MISSING = 'target_missing';

/** The four refusals of the merge, in the order the transaction applies them; null admits. */
export function precheck(anon: Row | null, target: Row | null, sameUser: boolean): MergeResult | null {
  if (sameUser) return refuse(SAME_USER, true);
  if (anon === null) return refuse(ANON_MISSING, true);
  if (!anon['is_anonymous']) return refuse(NOT_ANONYMOUS, false);
  if (target === null) return refuse(TARGET_MISSING, false);
  return null;
}

/** Ids the target account used to have, oldest first. */
export function previousUserIds(merges: readonly Row[], target: string): string[] {
  return merges
    .filter((m) => m['target_user_id'] === target && m['status'] === 'completed')
    .sort(byId)
    .map((m) => String(m['anon_user_id']));
}
