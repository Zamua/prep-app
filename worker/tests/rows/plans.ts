// The query plans behind a set of statements, judged by what bounds each
// loop. A loop may read a table whole only when the table cannot grow with
// use, and may walk an index range only when the range is bounded by
// something other than the account's size: the cards due now, one session's
// answers, a LIMIT that stops the walk. Everything else is a scan that grows
// with history, which is what makes a request's rows read grow with it.
//
// The LIMIT test is textual: a LIMIT anywhere in a statement whose plan sorts
// nothing counts as stopping its walks. That holds for every statement here,
// where each LIMIT closes the subquery or select that does the walking.
import type Database from 'better-sqlite3';
import type { Statement } from './recorder.js';

/** Tables a request may read whole: one row, or one row per deck or device. */
export const SMALL_TABLES = new Set(['profile', 'tombstone', 'schema_version', 'decks', 'push_subscriptions', 'byok_credentials']);

/** Index walks bounded by current work rather than by history, keyed by the
 * index and the constraint the plan walks it with, or by the index alone for a
 * partial index, whose every entry is current work. */
export const BOUNDED_WALKS: Record<string, string> = {
  'idx_cards_due (next_due<?)': 'the cards due now',
  'idx_cards_due (next_due>? AND next_due<?)': 'the due cards of one hour',
  idx_notifications_log_unseen: 'notifications not yet seen',
  idx_trivia_queue_unanswered: 'trivia not yet answered',
  'idx_active_workflows_type_deck (workflow_type=? AND deck_id>?)': 'refill jobs not yet pruned',
  'idx_sessions_status (status=?)': 'active sessions',
  'idx_sessions_status (status=? AND last_active<?)': 'active sessions',
  'idx_sessions_deck (deck_id=? AND status=?)': "one deck's active sessions",
  'idx_trivia_sessions_status (status=?)': 'active trivia sessions',
  'idx_trivia_sessions_deck_status (deck_id=? AND status=?)': "one deck's active trivia sessions",
  'sqlite_autoindex_study_session_answers_1 (session_id=?)': "one session's answers",
};

const toBinding = (v: unknown): unknown => (v && typeof v === 'object' && 'b64' in v ? Buffer.from(String((v as { b64: string }).b64), 'base64') : v);

/** `FROM t a`, `JOIN t AS a`: the alias a plan names a loop by, to its table. */
function aliases(sql: string): Map<string, string> {
  const out = new Map<string, string>();
  const keyword = /^(on|where|join|left|cross|inner|group|order|limit|indexed|using|set|values)$/i;
  for (const m of sql.matchAll(/\b(?:FROM|JOIN|UPDATE|INTO)\s+"?(\w+)"?(?:\s+(?:AS\s+)?(\w+))?/gi)) {
    out.set(m[1]!, m[1]!);
    if (m[2] && !keyword.test(m[2])) out.set(m[2], m[1]!);
  }
  return out;
}

function isScript(sql: string): boolean {
  return sql.replace(/--[^\n]*/g, '').trim().replace(/;\s*$/, '').includes(';');
}

/** Every loop no bound covers, as `table: plan step`, for these statements. */
export function unboundedLoops(db: Database.Database, statements: readonly Statement[]): string[] {
  const unique = new Map<string, { unique: boolean; columns: number }>();
  const indexOf = (name: string) => {
    let hit = unique.get(name);
    if (!hit) {
      const row = db.prepare("SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?").get(name) as { tbl_name: string } | undefined;
      const list = row ? (db.prepare(`SELECT name, "unique" FROM pragma_index_list(?)`).all(row.tbl_name) as { name: string; unique: number }[]) : [];
      const columns = (db.prepare('SELECT COUNT(*) AS n FROM pragma_index_info(?)').get(name) as { n: number }).n;
      hit = { unique: list.some((i) => i.name === name && i.unique === 1), columns };
      unique.set(name, hit);
    }
    return hit;
  };
  const found = new Set<string>();
  const seen = new Set<string>();
  for (const s of statements) {
    if (seen.has(s.sql) || isScript(s.sql)) continue;
    seen.add(s.sql);
    const steps = (db.prepare(`EXPLAIN QUERY PLAN ${s.sql}`).all(...s.bindings.map(toBinding)) as { detail: string }[]).map((r) => r.detail);
    const limited = /\bLIMIT\b/i.test(s.sql) && !steps.some((d) => d.includes('TEMP B-TREE'));
    const names = aliases(s.sql);
    for (const step of steps) {
      const scan = /^SCAN (\w+)(?: USING (?:COVERING )?INDEX (\w+))?$/.exec(step);
      if (scan) {
        const table = names.get(scan[1]!) ?? scan[1]!;
        if (!SMALL_TABLES.has(table) && !(scan[2] && (limited || BOUNDED_WALKS[scan[2]]))) found.add(`${table}: ${step}`);
        continue;
      }
      if (step.includes('AUTOMATIC')) {
        found.add(step);
        continue;
      }
      const byRowid = /^SEARCH (\w+) USING INTEGER PRIMARY KEY \((.*)\)$/.exec(step);
      if (byRowid) {
        if (byRowid[2] !== 'rowid=?' && !limited) found.add(`${names.get(byRowid[1]!) ?? byRowid[1]}: ${step}`);
        continue;
      }
      const search = /^SEARCH (\w+) USING (?:COVERING )?INDEX (\w+) \((.*)\)$/.exec(step);
      if (!search) continue;
      const [, alias, index, constraint] = search;
      const terms = constraint!.split(' AND ');
      const equalities = terms.filter((t) => /^\w+=\?$/.test(t)).length;
      const { unique: isUnique, columns } = indexOf(index!);
      if (isUnique && equalities === columns) continue;
      if (BOUNDED_WALKS[`${index} (${constraint})`] || BOUNDED_WALKS[index!] || limited) continue;
      found.add(`${names.get(alias!) ?? alias}: ${step}`);
    }
  }
  return [...found].sort();
}
