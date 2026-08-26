// A clock a test moves, and the alarms that fire against it. Cells under
// vitest hold real storage but no scheduler, so the scheduler is here: every
// cell registers, and `settle` fires whatever is due until nothing is, which
// is what the real alarm does over wall-clock time.
import type { FakeCellStorage } from './sqlStorage.js';

/** A clock the test moves; `tests/repos/setup.ts: MutableClock` is one. */
export interface SettableClock {
  now(): Date;
  set(at: Date): void;
}

interface Armed {
  storage: FakeCellStorage;
  fire(): Promise<void>;
}

export class AlarmBus {
  private readonly armed: Armed[] = [];

  constructor(readonly clock: SettableClock) {}

  /** Registers a cell whose `alarm()` this bus drives. A cell restarted over
   * the same storage replaces its predecessor, as a node restart does. */
  register(storage: FakeCellStorage, fire: () => Promise<void>): void {
    const existing = this.armed.findIndex((a) => a.storage === storage);
    if (existing === -1) this.armed.push({ storage, fire });
    else this.armed[existing] = { storage, fire };
  }

  /** Everything due within `slackMs` of now, moving the clock to each alarm
   * as wall time would. The slack covers the one-millisecond floor a due-now
   * wake is nudged past, and short retry backoffs; a gate deadline is not in
   * it, so a parked job stays parked. */
  async settle(slackMs = 1_000): Promise<number> {
    return this.settleThrough(this.clock.now().getTime() + slackMs);
  }

  /** Every alarm at or before now, oldest first, until none is due. */
  private async fireDue(maxFirings = 200): Promise<number> {
    let fired = 0;
    for (; fired < maxFirings; ) {
      const due = this.armed
        .filter((a) => a.storage.alarmAt !== null && a.storage.alarmAt <= this.clock.now().getTime())
        .sort((a, b) => (a.storage.alarmAt ?? 0) - (b.storage.alarmAt ?? 0));
      const next = due[0];
      if (!next) return fired;
      // Clearing before the handler is what the runtime does: a handler that
      // re-arms wins, and one that does not leaves the cell idle.
      next.storage.alarmAt = null;
      await next.fire();
      fired++;
    }
    throw new Error(`alarms did not settle in ${maxFirings} firings`);
  }

  /** Moves the clock to each armed alarm inside the window, firing as it
   * goes: the wait a gate deadline or a retry backoff would otherwise cost in
   * real seconds. */
  async settleThrough(untilMs: number, maxRounds = 400): Promise<number> {
    let fired = await this.fireDue();
    for (let round = 0; round < maxRounds; round++) {
      const next = this.earliest();
      if (next === null || next > untilMs) return fired;
      this.clock.set(new Date(next));
      fired += await this.fireDue();
    }
    throw new Error(`alarms did not settle through the window (earliest ${String(this.earliest())}, now ${this.clock.now().getTime()})`);
  }

  earliest(): number | null {
    const times = this.armed.map((a) => a.storage.alarmAt).filter((t): t is number => t !== null);
    return times.length ? Math.min(...times) : null;
  }

  pending(): number {
    return this.armed.filter((a) => a.storage.alarmAt !== null).length;
  }
}
