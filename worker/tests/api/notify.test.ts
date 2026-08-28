// Notification preferences, the device list, and the fanout: what is
// recorded, what is pruned, and what a bad preference answers with.
import { describe, expect, it, vi } from 'vitest';
import { DEFAULT_NOTIFICATION_PREFS } from '../../app/entities.js';
import { PrefsInvalid, validatePrefs } from '../../app/notify/prefs.js';
import * as notify from '../../app/notify/routes.js';
import type { PushOutcome, WebPush } from '../../app/ports.js';
import { cell } from '../repos/setup.js';

const deps = (webPush: WebPush = { send: async () => 'ok' }) => ({
  repos: cell().repos,
  webPush,
  vapidPublicKey: 'BCT1EPH4xriWIwlJllh05zjCEDDXMj0G',
});

describe('preference validation', () => {
  it('accepts the defaults', () => {
    expect(validatePrefs({ ...DEFAULT_NOTIFICATION_PREFS })).toEqual(DEFAULT_NOTIFICATION_PREFS);
  });

  it('names the allowed modes when one is unknown', () => {
    try {
      validatePrefs({ ...DEFAULT_NOTIFICATION_PREFS, mode: 'hourly' });
      expect.unreachable();
    } catch (e) {
      expect(e).toBeInstanceOf(PrefsInvalid);
      expect((e as PrefsInvalid).errors).toEqual([
        {
          type: 'enum',
          loc: ['mode'],
          msg: "Input should be 'off', 'digest' or 'when-ready'",
          input: 'hourly',
          ctx: { expected: "'off', 'digest' or 'when-ready'" },
        },
      ]);
    }
  });

  it('bounds every hour and the threshold', () => {
    const bad = (over: Record<string, unknown>) => {
      try {
        validatePrefs({ ...DEFAULT_NOTIFICATION_PREFS, ...over });
        return [];
      } catch (e) {
        return (e as PrefsInvalid).errors.map((x) => [x.loc[0], x.type]);
      }
    };
    expect(bad({ digest_hour: 24 })).toEqual([['digest_hour', 'less_than_equal']]);
    expect(bad({ quiet_start_hour: -1 })).toEqual([['quiet_start_hour', 'greater_than_equal']]);
    expect(bad({ threshold: 0 })).toEqual([['threshold', 'greater_than_equal']]);
    expect(bad({ threshold: 'many', tz: 5 })).toEqual([
      ['threshold', 'int_type'],
      ['tz', 'string_type'],
    ]);
  });
});

describe('POST /notify/prefs', () => {
  it('merges over the stored values and keeps the scheduler fields', () => {
    const d = deps();
    d.repos.prefs.setNotificationPrefs({ ...DEFAULT_NOTIFICATION_PREFS, threshold: 7, last_digest_date: '2026-03-01' });
    const result = notify.savePrefs(d, { mode: 'digest', digest_hour: 8, last_digest_date: '2020-01-01' }) as { json: { prefs: unknown } };
    expect(result.json.prefs).toEqual({ ...DEFAULT_NOTIFICATION_PREFS, mode: 'digest', digest_hour: 8, threshold: 7, last_digest_date: '2026-03-01' });
    expect(d.repos.prefs.getNotificationPrefs().last_digest_date).toBe('2026-03-01');
  });

  it('refuses a body that is not an object with a 400', () => {
    expect(notify.savePrefs(deps(), [1, 2])).toEqual({ json: { detail: 'expected an object' }, status: 400 });
  });
});

describe('subscriptions', () => {
  const SUB = { endpoint: 'https://push.example.test/sub', keys: { p256dh: 'p256', auth: 'auth' } };

  it('stores a device and counts it on the settings page', () => {
    const d = deps();
    expect(notify.subscribe(d, SUB)).toEqual({ json: { ok: true }, status: 200 });
    expect((notify.notifySettings(d) as unknown as { context: { devices: number } }).context.devices).toBe(1);
  });

  it('refuses a payload with no endpoint', () => {
    expect(notify.subscribe(deps(), { keys: {} })).toEqual({ json: { detail: 'bad subscription payload' }, status: 400 });
    expect(notify.unsubscribe(deps(), {})).toEqual({ json: { detail: 'missing endpoint' }, status: 400 });
  });

  it('forgets a device by its endpoint', () => {
    const d = deps();
    notify.subscribe(d, SUB);
    notify.unsubscribe(d, { endpoint: SUB.endpoint });
    expect(d.repos.pushSubs.count()).toBe(0);
  });
});

describe('the fanout', () => {
  it('logs before delivery, prunes what the service says is gone, and counts the rest', async () => {
    const outcomes: PushOutcome[] = ['ok', 'gone', 'fail'];
    const sent: string[] = [];
    const d = deps({
      send: async (sub, payload) => {
        sent.push(sub.endpoint);
        expect(JSON.parse(payload)).toMatchObject({ title: 'Prep', url: '/study/world-capitals' });
        return outcomes.shift()!;
      },
    });
    for (const n of [1, 2, 3]) d.repos.pushSubs.upsert(`https://push.example.test/${n}`, 'p', 'a');

    const counts = await notify.sendToUser(d, { title: 'Prep', body: 'one card is due', url: '/study/world-capitals', source: 'srs-digest' });
    expect(counts).toEqual({ sent: 1, failed: 1, pruned: 1 });
    expect(sent).toHaveLength(3);
    expect(d.repos.pushSubs.count()).toBe(2);
    expect(d.repos.notify.listRecent()[0]).toMatchObject({ title: 'Prep', url: '/study/world-capitals', source: 'srs-digest' });
  });

  it('records the attempt even when every device fails', async () => {
    const d = deps({ send: async () => 'fail' });
    d.repos.pushSubs.upsert('https://push.example.test/1', 'p', 'a');
    expect(await notify.sendTest(d)).toEqual({ json: { sent: 0, failed: 1, pruned: 0 }, status: 200 });
    expect(d.repos.notify.listRecent()[0]).toMatchObject({ title: 'Prep \u2014 test push', url: '/notify', source: 'manual' });
  });
});

describe('the notification log', () => {
  it('marks everything seen on render, so the unseen badge clears', () => {
    const d = deps();
    d.repos.notify.append({ title: 'a', body: 'b', url: '/', source: 'manual' });
    expect(d.repos.notify.countUnseen()).toBe(1);
    const rendered = notify.notificationLog(d) as unknown as { page: string; context: { entries: unknown[] } };
    expect(rendered.page).toBe('notify/log.html');
    expect(rendered.context.entries).toHaveLength(1);
    expect(d.repos.notify.countUnseen()).toBe(0);
  });
});

// The stub keeps vitest from leaking a real fetch if a future edit adds one.
vi.stubGlobal('fetch', async () => {
  throw new Error('the notify use cases must not reach the network');
});
