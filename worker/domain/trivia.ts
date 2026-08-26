// URL-encoded trivia session state: the remaining queue (`?cards=1,2,3`)
// and the verdict chain (`?done=1r,2w`).
import { pyStrip } from './py';

export type DoneVerdict = 'r' | 'w';
export type DoneItem = readonly [qid: number, verdict: DoneVerdict];

// Digits are ASCII and ids above MAX_SAFE_INTEGER are dropped.
const ID = /^[0-9]+$/;
const DONE = /^([0-9]+)([rw])$/;

function safeId(digits: string): number | null {
  const n = Number(digits);
  return n <= Number.MAX_SAFE_INTEGER ? n : null;
}

/** `?cards=1,2,3` to [1, 2, 3]; malformed chunks are dropped. */
export function parseCardIds(raw: string | null | undefined): number[] {
  if (!raw) return [];
  const out: number[] = [];
  for (const chunk of raw.split(',')) {
    const s = pyStrip(chunk);
    if (!ID.test(s)) continue;
    const id = safeId(s);
    if (id !== null) out.push(id);
  }
  return out;
}

/** `?done=42r,17w` to [[42, 'r'], [17, 'w']]; malformed chunks are dropped. */
export function parseDone(raw: string | null | undefined): DoneItem[] {
  if (!raw) return [];
  const out: DoneItem[] = [];
  for (const chunk of raw.split(',')) {
    const m = DONE.exec(pyStrip(chunk));
    if (!m) continue;
    const id = safeId(m[1]!);
    if (id !== null) out.push([id, m[2] as DoneVerdict]);
  }
  return out;
}

export function formatDone(items: readonly DoneItem[]): string {
  return items.map(([qid, verdict]) => `${qid}${verdict}`).join(',');
}

/** The chain with `qid`'s verdict replaced by the regrade outcome. */
export function flipDoneVerdict(items: readonly DoneItem[], qid: number, correct: boolean): string {
  const verdict: DoneVerdict = correct ? 'r' : 'w';
  return formatDone(items.map(([q, v]) => [q, q === qid ? verdict : v]));
}
