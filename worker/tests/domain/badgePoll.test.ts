// The masthead badge poll must not run forever against a session that is
// gone, back off on server errors, and stay quiet while the tab is hidden.
import { describe, expect, it } from 'vitest';

const MODULE = new URL('../../../static/js/modules/badge-poll.js', import.meta.url).href;

type Outcome = 'ok' | 'auth' | 'error';
interface Poller {
  start(): void;
  report(outcome: Outcome): void;
  visibility(hidden: boolean): void;
}
const poll = (await import(MODULE)) as {
  BASE_MS: number;
  MAX_MS: number;
  backoffMs(failures: number): number;
  classify(res: { status: number; responseURL: string }, badgePath: string): Outcome;
  createPoller(deps: {
    fire(): void;
    isHidden(): boolean;
    setTimer(fn: () => void, ms: number): number;
    clearTimer(id: number): void;
  }): Poller;
};
const { BASE_MS, MAX_MS, backoffMs, classify, createPoller } = poll;

/** A fake clock: timers fire only when the test advances it. */
function harness(hidden = false) {
  let now = 0;
  let nextId = 1;
  let isHidden = hidden;
  const timers = new Map<number, { at: number; fn: () => void }>();
  const fired: number[] = [];
  const poller = createPoller({
    fire: () => void fired.push(now),
    isHidden: () => isHidden,
    setTimer: (fn, ms) => {
      const id = nextId++;
      timers.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimer: (id) => void timers.delete(id),
  });
  return {
    poller,
    fired,
    pending: () => [...timers.values()].map((t) => t.at - now),
    advance(ms: number) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at;
        timers.delete(due[0]);
        due[1].fn();
      }
      now = end;
    },
    hide(h: boolean) {
      isHidden = h;
      poller.visibility(h);
    },
  };
}

describe('backoffMs', () => {
  it('doubles from the base per consecutive failure and stops at the cap', () => {
    expect(backoffMs(0)).toBe(BASE_MS);
    expect(backoffMs(1)).toBe(BASE_MS * 2);
    expect(backoffMs(2)).toBe(BASE_MS * 4);
    expect(backoffMs(50)).toBe(MAX_MS);
    for (let k = 0; k < 60; k++) expect(backoffMs(k)).toBeLessThanOrEqual(MAX_MS);
  });
});

describe('classify', () => {
  const path = '/api/active-workflows-badge';
  const url = (p: string) => `https://prep.example${p}`;

  it('is ok only for a 200 from the badge URL itself', () => {
    expect(classify({ status: 200, responseURL: url(path) }, path)).toBe('ok');
    expect(classify({ status: 200, responseURL: url(path) + '?x=1' }, path)).toBe('ok');
  });

  it('is auth for 401, 403 and for a redirect that landed elsewhere', () => {
    expect(classify({ status: 401, responseURL: url(path) }, path)).toBe('auth');
    expect(classify({ status: 403, responseURL: url(path) }, path)).toBe('auth');
    expect(classify({ status: 200, responseURL: url('/sign-in') }, path)).toBe('auth');
  });

  it('is error for everything else, including a network failure', () => {
    for (const status of [0, 204, 400, 404, 429, 500, 502, 503]) {
      expect(classify({ status, responseURL: url(path) }, path), String(status)).toBe('error');
    }
  });
});

describe('the poller', () => {
  it('fires at once on start, then every base interval while healthy', () => {
    const h = harness();
    h.poller.start();
    expect(h.fired).toEqual([0]);
    h.poller.report('ok');
    h.advance(BASE_MS - 1);
    expect(h.fired).toEqual([0]);
    h.advance(1);
    expect(h.fired).toEqual([0, BASE_MS]);
    h.poller.report('ok');
    h.advance(BASE_MS);
    expect(h.fired).toEqual([0, BASE_MS, 2 * BASE_MS]);
  });

  it('does not schedule the next poll until the last one reports', () => {
    const h = harness();
    h.poller.start();
    h.advance(10 * BASE_MS);
    expect(h.fired).toEqual([0]);
  });

  it('backs off exponentially on errors, caps, and resets on the first success', () => {
    const h = harness();
    h.poller.start();
    const gaps: number[] = [];
    for (let i = 0; i < 9; i++) {
      h.poller.report('error');
      gaps.push(h.pending()[0]!);
      h.advance(h.pending()[0]!);
    }
    expect(gaps).toEqual([10, 20, 40, 80, 160, 300, 300, 300, 300].map((n) => n * 1000));
    h.poller.report('ok');
    expect(h.pending()).toEqual([BASE_MS]);
  });

  it('stops when there is no badge left to poll', () => {
    let present = true;
    const timers: number[] = [];
    const poller = createPoller({
      fire: () => present,
      isHidden: () => false,
      setTimer: (_fn, ms) => timers.push(ms),
      clearTimer: () => {},
    });
    poller.start();
    poller.report('ok');
    expect(timers).toEqual([BASE_MS]);
    present = false;
    poller.visibility(true);
    poller.visibility(false);
    expect(timers).toEqual([BASE_MS]);
  });

  it('stops for good on an auth failure: no timer, no further polls', () => {
    const h = harness();
    h.poller.start();
    h.poller.report('auth');
    expect(h.pending()).toEqual([]);
    h.advance(24 * 60 * 60 * 1000);
    expect(h.fired).toEqual([0]);
  });

  it('a stale tab with a dead session sends one request, not thousands', () => {
    const h = harness();
    h.poller.start();
    h.poller.report('auth');
    h.advance(60 * 60 * 1000);
    expect(h.fired.length).toBe(1);
  });

  it('pauses while hidden and polls once, immediately, when shown again', () => {
    const h = harness();
    h.poller.start();
    h.poller.report('ok');
    h.advance(2000);
    h.hide(true);
    expect(h.pending()).toEqual([]);
    h.advance(60 * 60 * 1000);
    expect(h.fired).toEqual([0]);
    h.hide(false);
    expect(h.fired.length).toBe(2);
    h.poller.report('ok');
    expect(h.pending()).toEqual([BASE_MS]);
  });

  it('a response that lands while hidden does not restart the timer', () => {
    const h = harness();
    h.poller.start();
    h.hide(true);
    h.poller.report('ok');
    expect(h.pending()).toEqual([]);
    h.advance(60 * 60 * 1000);
    expect(h.fired).toEqual([0]);
  });

  it('start while hidden fires nothing until the tab is shown', () => {
    const h = harness(true);
    h.poller.start();
    expect(h.fired).toEqual([]);
    h.hide(false);
    expect(h.fired.length).toBe(1);
  });

  it('showing the tab again probes a stopped poller once and stops again on a 401', () => {
    const h = harness();
    h.poller.start();
    h.poller.report('auth');
    h.hide(true);
    h.hide(false);
    expect(h.fired.length).toBe(2);
    h.poller.report('auth');
    expect(h.pending()).toEqual([]);
  });

  it('showing the tab while a request is in flight does not fire a second one', () => {
    const h = harness();
    h.poller.start();
    h.hide(true);
    h.hide(false);
    expect(h.fired.length).toBe(1);
  });

  it('a backed-off poller keeps its backoff across a hide and show', () => {
    const h = harness();
    h.poller.start();
    h.poller.report('error');
    h.poller.report('error');
    h.hide(true);
    h.hide(false);
    h.poller.report('error');
    expect(h.pending()).toEqual([backoffMs(3)]);
  });
});
