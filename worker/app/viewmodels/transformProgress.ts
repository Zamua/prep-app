// The per-deck groupings `partials/transform_progress.html` reads under
// the reorganize scope, as ordered `[label, members]` entries.

export interface TransformProgressContext {
  progress?: { plan?: TransformPlan | null } | null;
  modification_diffs?: ModificationDiff[] | null;
  deletion_decks?: Record<string, string> | null;
  move_source_decks?: Record<string, string> | null;
}

export interface TransformPlan {
  additions?: Addition[] | null;
  deletions?: Array<number | string> | null;
  card_moves?: CardMove[] | null;
}

export interface ModificationDiff {
  deck_name?: string | null;
}

export interface Addition {
  dest_deck?: string | null;
}

export interface CardMove {
  question_id: number | string;
  dest_deck?: string | null;
}

export type Groups<T> = Array<[string, T[]]>;

const UNKNOWN = "(unknown)";

function groupBy<T>(members: T[], key: (member: T) => string): Groups<T> {
  const groups = new Map<string, T[]>();
  for (const member of members) {
    const k = key(member);
    const bucket = groups.get(k);
    if (bucket) bucket.push(member);
    else groups.set(k, [member]);
  }
  return [...groups.entries()];
}

function lookup(table: Record<string, string> | null | undefined, id: number | string): string {
  const value = table?.[String(id)];
  return value ? value : UNKNOWN;
}

export interface TransformProgressFields {
  mods_by_deck: Groups<ModificationDiff>;
  adds_by_deck: Groups<Addition>;
  dels_by_deck: Groups<number | string>;
  move_groups: Groups<number | string>;
}

export function deriveTransformProgress(context: TransformProgressContext): TransformProgressFields {
  const plan = context.progress?.plan ?? null;
  const moves = plan?.card_moves ?? [];
  return {
    mods_by_deck: groupBy(context.modification_diffs ?? [], (d) => d.deck_name || UNKNOWN),
    adds_by_deck: groupBy(plan?.additions ?? [], (a) => a.dest_deck || UNKNOWN),
    dels_by_deck: groupBy(plan?.deletions ?? [], (qid) => lookup(context.deletion_decks, qid)),
    move_groups: groupBy(moves, (mv) => `${lookup(context.move_source_decks, mv.question_id)} → ${mv.dest_deck}`).map(
      ([label, members]) => [label, members.map((mv) => mv.question_id)],
    ),
  };
}
