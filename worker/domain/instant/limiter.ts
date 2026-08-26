// The instant limiter's windows over the generation ledger. Spend outcomes
// (`pending`, `ok`, `failed_spent`) count toward every window; `failed_free`
// refusals count toward the per-IP burst window only. Per-IP windows are
// the anti-Sybil lever, per-user windows the anti-NAT lever; both must pass.
import { parseIso } from '../py.js';

// The per-minute cap's window defines what "per minute" means; the burst
// window is a limit like the others.
export const MINUTE_WINDOW_S = 60;
export const DAY_WINDOW_S = 86400;
export const RETENTION_DAYS = 7;

export interface Limits {
  /** Per-IP rows of any outcome per burst window. */
  burstLimit: number;
  burstWindowS: number;
  perIpPerDay: number;
  perAnonUserPerDay: number;
  perUserPerDay: number;
  globalPerDay: number;
  globalPerMinute: number;
}

export const DEFAULT_LIMITS: Limits = {
  burstLimit: 1,
  burstWindowS: 60,
  perIpPerDay: 3,
  perAnonUserPerDay: 3,
  perUserPerDay: 20,
  globalPerDay: 200,
  globalPerMinute: 4,
};

export const SPEND_OUTCOMES = ['pending', 'ok', 'failed_spent'] as const;
export const TERMINAL_OUTCOMES = ['ok', 'failed_spent', 'failed_free'] as const;

export interface GenerationRow {
  ip: string;
  created_at: string | null;
  outcome: string;
  user_id: string | null;
}

/** `minute` and `day` are rate-limited scopes; `busy` is a tripped global cap. */
export interface Refusal {
  kind: 'minute' | 'day' | 'busy';
  retryAfterS: number | null;
}

export interface WindowRequest {
  ip: string;
  /** The account charged, or null for the request that mints one. */
  userId: string | null;
  /** null: no users row, which takes the anonymous budget. */
  userIsAnonymous: boolean | null;
  at: Date;
}

/** Seconds until the row ages out of its window; the whole window when unknown. */
export function retryAfter(at: Date, createdAtIso: string | null, windowS: number): number {
  if (!createdAtIso) return windowS;
  let ts: Date;
  try {
    ts = parseIso(createdAtIso);
  } catch {
    return windowS;
  }
  const remaining = windowS - (at.getTime() - ts.getTime()) / 1000;
  return Math.max(1, Math.ceil(remaining));
}

interface Stamped {
  row: GenerationRow;
  t: number;
}

const isSpend = (r: GenerationRow) => (SPEND_OUTCOMES as readonly string[]).includes(r.outcome);

function stamp(rows: readonly GenerationRow[]): Stamped[] {
  const out: Stamped[] = [];
  for (const row of rows) {
    if (!row.created_at) continue;
    try {
      out.push({ row, t: parseIso(row.created_at).getTime() });
    } catch {
      // A row with no readable instant is in no window.
    }
  }
  return out;
}

/**
 * The first window `rows` trips for the request, or null to admit. Rows
 * are the ledger as the cell holds it; the reservation insert is its own.
 */
export function checkWindows(
  rows: readonly GenerationRow[],
  req: WindowRequest,
  limits: Limits = DEFAULT_LIMITS,
): Refusal | null {
  const now = req.at.getTime();
  const since = (windowS: number) => now - windowS * 1000;
  const stamped = stamp(rows);

  const burst = stamped.filter((s) => s.row.ip === req.ip && s.t >= since(limits.burstWindowS));
  if (burst.length >= limits.burstLimit) {
    const newest = burst.reduce<Stamped | null>((best, s) => (best === null || s.t > best.t ? s : best), null);
    return { kind: 'minute', retryAfterS: retryAfter(req.at, newest?.row.created_at ?? null, limits.burstWindowS) };
  }

  const dayWindow = (pick: (r: GenerationRow) => boolean, limit: number): Refusal | null => {
    const inWindow = stamped.filter((s) => pick(s.row) && isSpend(s.row) && s.t >= since(DAY_WINDOW_S));
    if (inWindow.length < limit) return null;
    const oldest = [...inWindow].sort((a, b) => a.t - b.t)[inWindow.length - limit];
    return { kind: 'day', retryAfterS: retryAfter(req.at, oldest?.row.created_at ?? null, DAY_WINDOW_S) };
  };

  const ipDay = dayWindow((r) => r.ip === req.ip, limits.perIpPerDay);
  if (ipDay) return ipDay;

  if (req.userId !== null) {
    const limit = req.userIsAnonymous === false ? limits.perUserPerDay : limits.perAnonUserPerDay;
    const userDay = dayWindow((r) => r.user_id === req.userId, limit);
    if (userDay) return userDay;
  }

  const spend = stamped.filter((s) => isSpend(s.row));
  if (spend.filter((s) => s.t >= since(MINUTE_WINDOW_S)).length >= limits.globalPerMinute) {
    return { kind: 'busy', retryAfterS: null };
  }
  if (spend.filter((s) => s.t >= since(DAY_WINDOW_S)).length >= limits.globalPerDay) {
    return { kind: 'busy', retryAfterS: null };
  }
  return null;
}
