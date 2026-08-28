// When a user's cell must next wake, and what is due when it does. Pure, so
// a cold cell, an evicted cell and a duplicate alarm all reach the same
// answer: the alarm is re-derived from the rows, never held.
//
// An alarm cannot decline to act and simply come back the way a periodic
// tick could, so every path that declines names the instant it wants
// instead.
import { isoUtc, parseIso } from '../time.js';

const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;

/** `_WHEN_READY_DEBOUNCE_SECONDS`. */
export const WHEN_READY_DEBOUNCE_MS = 4 * HOUR_MS;
/** `_DEFAULT_INTERVAL_MINUTES` for a trivia deck with no interval set. */
export const DEFAULT_TRIVIA_INTERVAL_MINUTES = 30;
/** `_MAX_BACKOFF_DOUBLINGS`: the effective interval is `base * 2 ** min(streak, 5)`. */
export const MAX_BACKOFF_DOUBLINGS = 5;
/** `_RESUME_NOTIF_IDLE_MIN`: someone still answering needs no reminder. */
export const RESUME_IDLE_MS = 5 * MINUTE_MS;
/** The reconciler's 24h prune of terminal rows, now one task of this alarm. */
export const TERMINAL_PRUNE_MS = 24 * HOUR_MS;
export const DEFAULT_TZ = 'America/New_York';

/** The scheduler-visible half of `NotificationPrefs`, structurally. */
export interface WakePrefs {
  mode: string;
  digest_hour: number;
  tz: string;
  threshold: number;
  quiet_hours_enabled: boolean;
  quiet_start_hour: number;
  quiet_end_hour: number;
  last_digest_date: string | null;
  last_when_ready_at: string | null;
}

export interface TriviaDeckState {
  id: number;
  notificationsEnabled: boolean;
  mutedUntil: string | null;
  intervalMinutes: number | null;
  ignoredStreak: number;
  lastNotifiedAt: string | null;
  sessionSize: number;
  /** Queue rows never answered: the refill gate. */
  unanswered: number;
  /** Queue rows at all; with none there is nothing to pick and nothing to say. */
  queued: number;
  /** `context_prompt` or the slug, stripped; an empty one cannot be refilled. */
  topic: string;
  /** When this deck's most recent TriviaGenerate job started, in flight or
   * not: the refill's own guard, and the row a dispatch writes. */
  lastRefillAt: string | null;
  /** `last_active` of an active session that still holds a queue. */
  activeSince: string | null;
}

export interface WakeInputs {
  prefs: WakePrefs;
  /** Whether a tier funds an LLM call and this deploy runs jobs at all. With
   * neither, a refill is not a task that could ever complete, so planning one
   * would leave the deck asking on every wake for the life of the deploy. */
  canGenerate: boolean;
  /** No device means nothing to push to, so nothing is planned. */
  hasPushDevice: boolean;
  dueTotal: number;
  /** When the next SRS card comes due; null when none is scheduled. */
  nextDueAt: string | null;
  decks: readonly TriviaDeckState[];
  /** The oldest terminal `active_workflows` row: the prune's clock. */
  earliestTerminalAt: string | null;
}

export type WakeTask =
  | { kind: 'digest'; localDate: string }
  | { kind: 'when-ready' }
  | { kind: 'trivia-refill'; deckId: number }
  | { kind: 'trivia-notify'; deckId: number }
  | { kind: 'prune' };

export interface WakePlan {
  tasks: WakeTask[];
  /** Null when nothing is outstanding: the cell sleeps until a request. */
  wakeAt: string | null;
}

/**
 * What is due now and when to come back. A task in `tasks` always writes the
 * stamp its own guard reads, so running the plan and re-planning moves the
 * wake forward; that property is what keeps a duplicate fire a no-op.
 */
export function planWake(inputs: WakeInputs, now: Date): WakePlan {
  const tasks: WakeTask[] = [];
  const wakes: number[] = [];
  const quietEnd = quietEndsAt(inputs.prefs, now);

  if (inputs.hasPushDevice && inputs.prefs.mode === 'digest') planDigest(inputs, now, tasks, wakes);
  if (inputs.hasPushDevice && inputs.prefs.mode === 'when-ready') planWhenReady(inputs, now, quietEnd, tasks, wakes);
  // Trivia is per deck and ignores `mode`, which governs the SRS pair only.
  for (const deck of inputs.decks) planDeck(deck, inputs.canGenerate, now, quietEnd, tasks, wakes);
  planPrune(inputs, now, tasks, wakes);

  if (tasks.length) return { tasks, wakeAt: isoUtc(now) };
  const ahead = wakes.filter((t) => t > now.getTime());
  return { tasks, wakeAt: ahead.length ? isoUtc(new Date(Math.min(...ahead))) : null };
}

/** The digest fires at the chosen hour, once per local date. Quiet hours do
 * not apply: the chosen hour is the schedule. */
function planDigest(i: WakeInputs, now: Date, tasks: WakeTask[], wakes: number[]): void {
  const tz = i.prefs.tz || DEFAULT_TZ;
  const parts = partsIn(tz, now);
  const today = localDate(parts);
  const inWindow = parts.hour === i.prefs.digest_hour && i.prefs.last_digest_date !== today;
  if (inWindow && i.dueTotal > 0) {
    tasks.push({ kind: 'digest', localDate: today });
    return;
  }
  const nextHour = nextLocalHour(tz, i.prefs.digest_hour, now).getTime();
  // Inside the window with nothing due yet: a card that comes due before the
  // hour is out still gets the digest, as the five-minute tick gave it.
  const due = inWindow ? tryParse(i.nextDueAt) : null;
  if (due !== null && due > now.getTime() && due < nextHour) wakes.push(due);
  else wakes.push(nextHour);
}

function planWhenReady(i: WakeInputs, now: Date, quietEnd: Date | null, tasks: WakeTask[], wakes: number[]): void {
  const at = now.getTime();
  // An unparsable stamp is no debounce: a damaged row must not silence
  // notifications forever.
  const last = tryParse(i.prefs.last_when_ready_at);
  const debounceEnd = last === null ? at : last + WHEN_READY_DEBOUNCE_MS;
  const readyAt = i.dueTotal >= i.prefs.threshold ? at : tryParse(i.nextDueAt);
  // Nothing due and nothing coming: no wake belongs to this mode at all.
  if (readyAt === null) return;
  let wake = Math.max(at, readyAt, debounceEnd);
  if (quietEnd !== null) wake = Math.max(wake, quietEnd.getTime());
  if (wake <= at) tasks.push({ kind: 'when-ready' });
  else wakes.push(wake);
}

function planDeck(d: TriviaDeckState, canGenerate: boolean, now: Date, quietEnd: Date | null, tasks: WakeTask[], wakes: number[]): void {
  if (!d.notificationsEnabled) return;
  const at = now.getTime();
  // The stamps are fixed-width UTC, so a string compare is a time
  // compare.
  if (d.mutedUntil !== null && d.mutedUntil > isoUtc(now)) {
    const until = tryParse(d.mutedUntil);
    if (until !== null && until > at) wakes.push(until);
    return;
  }
  const due = triviaDueAt(d, now).getTime();
  if (due > at) {
    wakes.push(due);
    return;
  }

  // The refill is a dispatch, never a generation: the alarm does not call the
  // LLM. It runs during quiet hours so morning has fresh content, and no more
  // often than the deck's own interval.
  // Its guard is the job row the dispatch writes; without one, a deck that
  // cannot be refilled would ask again on every wake, forever.
  if (canGenerate && d.unanswered < d.sessionSize && d.topic !== '') {
    const refillAt = refillDueAt(d, now).getTime();
    if (refillAt <= at) tasks.push({ kind: 'trivia-refill', deckId: d.id });
    else wakes.push(refillAt);
  }

  // Quiet hours skip the fire and leave `last_notified_at` alone, so the deck
  // fires the moment the window reopens.
  if (quietEnd !== null) {
    wakes.push(quietEnd.getTime());
    return;
  }
  if (d.activeSince !== null) {
    const idleUntil = (tryParse(d.activeSince) ?? at) + RESUME_IDLE_MS;
    if (idleUntil > at) {
      wakes.push(idleUntil);
      return;
    }
  }
  // Nothing to pick. A refill's own status write re-arms this cell when it
  // lands, so the deck is not left waiting on a wake it cannot justify.
  if (d.queued === 0) return;
  tasks.push({ kind: 'trivia-notify', deckId: d.id });
}

function planPrune(i: WakeInputs, now: Date, tasks: WakeTask[], wakes: number[]): void {
  const oldest = tryParse(i.earliestTerminalAt);
  if (oldest === null) return;
  const at = oldest + TERMINAL_PRUNE_MS;
  if (at <= now.getTime()) tasks.push({ kind: 'prune' });
  else wakes.push(at);
}

// ---- the pieces the tasks share -------------------------------------------

/** `[start, end)` local, wrapping midnight. An equal pair silences nothing,
 * so a misconfigured range cannot mute everything. */
export function inQuietHours(localHour: number, start: number, end: number): boolean {
  if (start === end) return false;
  if (start < end) return start <= localHour && localHour < end;
  return localHour >= start || localHour < end;
}

/** When the quiet window `now` sits in reopens; null when it is not quiet. */
export function quietEndsAt(prefs: WakePrefs, now: Date): Date | null {
  if (!prefs.quiet_hours_enabled) return null;
  const tz = prefs.tz || DEFAULT_TZ;
  if (!inQuietHours(partsIn(tz, now).hour, prefs.quiet_start_hour, prefs.quiet_end_hour)) return null;
  return nextLocalHour(tz, prefs.quiet_end_hour, now);
}

export function effectiveIntervalMinutes(baseMinutes: number, ignoredStreak: number): number {
  return baseMinutes * 2 ** Math.max(0, Math.min(ignoredStreak, MAX_BACKOFF_DOUBLINGS));
}

/** When the deck next owes a question; an unset or unparsable stamp is
 * due now. */
export function triviaDueAt(deck: TriviaDeckState, now: Date): Date {
  return dueAt(deck, deck.lastNotifiedAt, now);
}

/** The same cadence for the refill, off the job row a dispatch writes rather
 * than off `last_notified_at`, which quiet hours deliberately leave alone. */
export function refillDueAt(deck: TriviaDeckState, now: Date): Date {
  return dueAt(deck, deck.lastRefillAt, now);
}

function dueAt(deck: TriviaDeckState, stamp: string | null, now: Date): Date {
  const last = tryParse(stamp);
  if (last === null) return now;
  const base = deck.intervalMinutes || DEFAULT_TRIVIA_INTERVAL_MINUTES;
  return new Date(last + effectiveIntervalMinutes(base, deck.ignoredStreak) * MINUTE_MS);
}

// ---- local time -------------------------------------------------------------

export interface LocalParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

// One formatter per zone, shared by every cell an isolate serves: a cache,
// which is the only thing module state may be.
const formatters = new Map<string, Intl.DateTimeFormat | null>();

function formatterFor(tz: string): Intl.DateTimeFormat | null {
  const key = tz || DEFAULT_TZ;
  const memo = formatters.get(key);
  if (memo !== undefined) return memo;
  // An unknown or malformed zone falls back to the default rather than
  // throwing: a bad tz pref must not take the whole wake down.
  const made = buildFormatter(key) ?? buildFormatter(DEFAULT_TZ);
  formatters.set(key, made);
  return made;
}

function buildFormatter(tz: string): Intl.DateTimeFormat | null {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return null;
  }
}

/** The wall-clock fields of `at` in `tz`. A runtime with no zone data leaves
 * local time equal to UTC rather than refusing to schedule anything. */
export function partsIn(tz: string, at: Date): LocalParts {
  const fmt = formatterFor(tz);
  if (fmt === null) {
    return {
      year: at.getUTCFullYear(),
      month: at.getUTCMonth() + 1,
      day: at.getUTCDate(),
      hour: at.getUTCHours(),
      minute: at.getUTCMinutes(),
      second: at.getUTCSeconds(),
    };
  }
  const p: Record<string, string> = {};
  for (const part of fmt.formatToParts(at)) p[part.type] = part.value;
  const hour = Number(p['hour']);
  return {
    year: Number(p['year']),
    month: Number(p['month']),
    day: Number(p['day']),
    hour: hour === 24 ? 0 : hour,
    minute: Number(p['minute']),
    second: Number(p['second']),
  };
}

export function localDate(p: LocalParts): string {
  return `${String(p.year).padStart(4, '0')}-${String(p.month).padStart(2, '0')}-${String(p.day).padStart(2, '0')}`;
}

function offsetAt(tz: string, at: Date): number {
  const p = partsIn(tz, at);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - at.getTime();
}

/** The instant whose wall time in `tz` is that date at `hour:00`. Two passes,
 * because the offset either side of the guess differs across a DST change. */
function instantOfLocal(tz: string, year: number, month: number, day: number, hour: number): Date {
  const wall = Date.UTC(year, month - 1, day, hour);
  let t = wall - offsetAt(tz, new Date(wall));
  t = wall - offsetAt(tz, new Date(t));
  return new Date(t);
}

/** The next instant whose local hour is `hour` and minute 0, strictly after
 * `now`. A spring-forward that skips the hour lands on the hour after it, and
 * the task's own hour check then pushes the wake to the following day. */
export function nextLocalHour(tz: string, hour: number, now: Date): Date {
  const p = partsIn(tz, now);
  for (let day = 0; day <= 3; day++) {
    const on = new Date(Date.UTC(p.year, p.month - 1, p.day + day));
    const at = instantOfLocal(tz, on.getUTCFullYear(), on.getUTCMonth() + 1, on.getUTCDate(), hour);
    if (at.getTime() > now.getTime()) return at;
  }
  return new Date(now.getTime() + 86_400_000);
}

/** Epoch milliseconds, or null for a missing or malformed stamp. */
function tryParse(iso: string | null): number | null {
  if (!iso) return null;
  try {
    return parseIso(iso).getTime();
  } catch {
    return null;
  }
}
