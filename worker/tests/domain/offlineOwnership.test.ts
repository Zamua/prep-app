// Whose offline data a device holds, driven by the event sequences the sync
// loop raises. A wrong move here replays one account's outbox into another.
import { describe, expect, it } from 'vitest';

const MODULE = new URL('../../../static/js/offline/ownership.js', import.meta.url).href;

type User = { id: string; display_name?: string; previous_ids?: string[] };
type Owner = { user_id: string; display_name: string };
type State =
  | { kind: 'unclaimed' }
  | { kind: 'wiped' }
  | { kind: 'owned'; owner: Owner }
  | { kind: 'conflict'; owner: Owner; serverUser: User };
type Event =
  | { type: 'refreshing' }
  | { type: 'flushing'; localCards: number }
  | { type: 'signedIn'; user: User }
  | { type: 'claim' }
  | { type: 'stamped'; owner: Owner }
  | { type: 'wiped' }
  | { type: 'kept' };
type Effect = { type: string; [k: string]: unknown };
interface Move {
  state: State;
  effects: Effect[];
  refused: string | null;
}

const ownership = (await import(MODULE)) as {
  ownershipOf(meta: { owner?: unknown; wiped?: unknown }): State;
  next(state: State, event: Event): Move;
  shouldPrompt(state: State, dismissed: unknown): boolean;
};
const { next, ownershipOf, shouldPrompt } = ownership;

const A: Owner = { user_id: 'user_a', display_name: 'A' };
const owner = (id: string): Owner => ({ user_id: id, display_name: id });

/** Every move in order, the device carried from one to the next. */
function run(from: State, events: Event[]): Move[] {
  const moves: Move[] = [];
  let s = from;
  for (const e of events) {
    const m = next(s, e);
    moves.push(m);
    s = m.state;
  }
  return moves;
}

/** A snapshot refresh as sync.js raises it, for `user`. */
const refresh = (user: User): Event[] => [{ type: 'refreshing' }, { type: 'signedIn', user }, { type: 'claim' }, { type: 'stamped', owner: owner(user.id) }];

describe('device ownership', () => {
  it('reads the meta records as a state', () => {
    expect(ownershipOf({})).toEqual({ kind: 'unclaimed' });
    expect(ownershipOf({ wiped: { at: 'x' } })).toEqual({ kind: 'wiped' });
    expect(ownershipOf({ owner: A })).toEqual({ kind: 'owned', owner: A });
  });

  it('a fresh device is adopted by the first snapshot, then syncs for that account only', () => {
    const moves = run({ kind: 'unclaimed' }, [...refresh({ id: 'user_a' }), { type: 'flushing', localCards: 2 }, { type: 'signedIn', user: { id: 'user_a' } }]);
    expect(moves.every((m) => m.refused === null)).toBe(true);
    expect(moves[3]!.effects).toEqual([{ type: 'stamp', owner: owner('user_a') }]);
    expect(moves.at(-1)!.state).toEqual({ kind: 'owned', owner: owner('user_a') });
  });

  it('holds back offline-authored cards on an unstamped device until a refresh stamps it', () => {
    expect(next({ kind: 'unclaimed' }, { type: 'flushing', localCards: 1 }).refused).toBe('unstamped');
    expect(next({ kind: 'unclaimed' }, { type: 'flushing', localCards: 0 }).refused).toBeNull();
  });

  it('turns sync off for a different account, and keeps it off for the page', () => {
    const [mismatch, flush, again, refreshAgain, stamp] = run({ kind: 'owned', owner: A }, [
      { type: 'signedIn', user: { id: 'user_b' } },
      { type: 'flushing', localCards: 0 },
      { type: 'signedIn', user: { id: 'user_a' } },
      { type: 'refreshing' },
      { type: 'stamped', owner: owner('user_b') },
    ]);
    expect(mismatch).toEqual({ state: { kind: 'conflict', owner: A, serverUser: { id: 'user_b' } }, effects: [], refused: 'mismatch' });
    for (const m of [flush, again, refreshAgain, stamp]) expect(m!.refused).toBe('disabled');
    expect(stamp!.effects).toEqual([]);
  });

  it('re-stamps an account that absorbed the owner in a merge', () => {
    const m = next({ kind: 'owned', owner: A }, { type: 'signedIn', user: { id: 'user_b', previous_ids: ['user_a'] } });
    expect(m.refused).toBeNull();
    expect(m.effects).toEqual([{ type: 'stamp', owner: { ...A, user_id: 'user_b' } }]);
    expect(m.state).toEqual({ kind: 'owned', owner: { ...A, user_id: 'user_b' } });
  });

  it('remembers keep for the account that chose it, and asks any other', () => {
    const conflict = next({ kind: 'owned', owner: A }, { type: 'signedIn', user: { id: 'user_b' } }).state;
    expect(shouldPrompt(conflict, null)).toBe(true);
    expect(next(conflict, { type: 'kept' }).effects).toEqual([{ type: 'recordKeep', userId: 'user_b' }]);
    expect(shouldPrompt(conflict, { dismissed_user_id: 'user_b' })).toBe(false);
    expect(shouldPrompt(conflict, { dismissed_user_id: 'user_c' })).toBe(true);
    expect(shouldPrompt({ kind: 'owned', owner: A }, null)).toBe(false);
  });

  it('a wipe leaves nothing adoptable: orphan rows are dropped, never flushed, until a new owner is stamped', () => {
    const conflict = next({ kind: 'owned', owner: A }, { type: 'signedIn', user: { id: 'user_b' } }).state;
    const [wiped, flush, ...reseed] = run(conflict, [{ type: 'wiped' }, { type: 'flushing', localCards: 3 }, ...refresh({ id: 'user_b' })]);
    expect(wiped!.state).toEqual({ kind: 'wiped' });
    expect(flush).toMatchObject({ refused: 'wiped', effects: [{ type: 'dropOrphans' }] });
    const [, signedIn, claim, stamped] = reseed;
    expect(signedIn!.refused).toBeNull();
    expect(claim).toEqual({ state: { kind: 'unclaimed' }, effects: [{ type: 'dropOrphans' }, { type: 'clearWiped' }], refused: null });
    expect(stamped!.state).toEqual({ kind: 'owned', owner: owner('user_b') });
  });

  it('decides every state and event', () => {
    const states: State[] = [{ kind: 'unclaimed' }, { kind: 'wiped' }, { kind: 'owned', owner: A }, { kind: 'conflict', owner: A, serverUser: { id: 'user_b' } }];
    const events: Event[] = [
      { type: 'refreshing' },
      { type: 'flushing', localCards: 1 },
      { type: 'signedIn', user: { id: 'user_b' } },
      { type: 'claim' },
      { type: 'stamped', owner: A },
      { type: 'wiped' },
      { type: 'kept' },
    ];
    for (const s of states) {
      for (const e of events) {
        const m = next(s, e);
        expect(m.state, `${e.type} from ${s.kind}`).toBeDefined();
        expect(Array.isArray(m.effects)).toBe(true);
      }
    }
    expect(() => next({ kind: 'unclaimed' }, { type: 'nope' } as unknown as Event)).toThrow(/unknown ownership event/);
  });
});
