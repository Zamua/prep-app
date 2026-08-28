// The notify surface and the push fanout. Delivery itself is the `WebPush`
// port; this layer decides what is sent and what is recorded.
import { DEFAULT_NOTIFICATION_PREFS } from '../entities.js';
import { detail, json, type ApiResult } from '../http.js';
import type { UserRepos, WebPush } from '../ports.js';
import { PrefsInvalid, validatePrefs } from './prefs.js';

export interface NotifyDeps {
  repos: UserRepos;
  webPush: WebPush;
  vapidPublicKey: string;
}

export const LOG_LIMIT = 50;

/** Opening the page IS the "I saw these" gesture. */
export function notificationLog(deps: NotifyDeps): ApiResult {
  const entries = deps.repos.notify.listRecent(LOG_LIMIT);
  deps.repos.notify.markAllSeen();
  return { page: 'notify/log.html', context: { entries } };
}

export function notifySettings(deps: NotifyDeps): ApiResult {
  return {
    page: 'notify_settings.html',
    context: {
      prefs: deps.repos.prefs.getNotificationPrefs(),
      devices: deps.repos.pushSubs.count(),
      vapid_key: deps.vapidPublicKey,
    },
  };
}

/** Merge over the stored prefs so the scheduler-only fields survive. */
export function savePrefs(deps: NotifyDeps, body: unknown): ApiResult {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return detail(400, 'expected an object');
  const existing = deps.repos.prefs.getNotificationPrefs();
  const merged: Record<string, unknown> = { ...DEFAULT_NOTIFICATION_PREFS, ...existing, ...(body as Record<string, unknown>) };
  // Scheduler-managed state stays untouched even when the client sends it.
  merged['last_digest_date'] = existing.last_digest_date;
  merged['last_when_ready_at'] = existing.last_when_ready_at;
  let prefs;
  try {
    prefs = validatePrefs(merged);
  } catch (e) {
    if (e instanceof PrefsInvalid) return detail(422, e.errors);
    throw e;
  }
  deps.repos.prefs.setNotificationPrefs(prefs);
  return json({ ok: true, prefs });
}

export function subscribe(deps: NotifyDeps, body: unknown): ApiResult {
  if (typeof body !== 'object' || body === null || Array.isArray(body) || !('endpoint' in (body as object))) {
    return detail(400, 'bad subscription payload');
  }
  const sub = body as Record<string, unknown>;
  const keys = (sub['keys'] ?? {}) as Record<string, unknown>;
  const endpoint = sub['endpoint'];
  const p256dh = keys['p256dh'];
  const auth = keys['auth'];
  if (!endpoint || !p256dh || !auth) return detail(400, 'subscription missing endpoint/keys.p256dh/keys.auth');
  deps.repos.pushSubs.upsert(String(endpoint), String(p256dh), String(auth));
  return json({ ok: true });
}

/** The endpoint is the natural key: it can only belong to one user. */
export function unsubscribe(deps: NotifyDeps, body: unknown): ApiResult {
  const endpoint = typeof body === 'object' && body !== null && !Array.isArray(body) ? (body as Record<string, unknown>)['endpoint'] : null;
  if (!endpoint) return detail(400, 'missing endpoint');
  deps.repos.pushSubs.deleteByEndpoint(String(endpoint));
  return json({ ok: true });
}

export interface FanoutCounts {
  sent: number;
  failed: number;
  pruned: number;
}

/**
 * Push to every device the user has subscribed, pruning the ones the push
 * service rejects. The log row is appended before delivery so a failure
 * still leaves a record of what was attempted.
 */
export async function sendToUser(
  deps: NotifyDeps,
  message: { title: string; body: string; url?: string | null; source?: string; tag?: string | null },
): Promise<FanoutCounts> {
  const url = message.url || '/';
  const payload: Record<string, unknown> = { title: message.title, body: message.body, url };
  if (message.tag) payload['tag'] = message.tag;
  deps.repos.notify.append({ title: message.title, body: message.body, url, source: message.source ?? 'manual' });
  const body = JSON.stringify(payload);
  let sent = 0;
  let failed = 0;
  let pruned = 0;
  for (const sub of deps.repos.pushSubs.list()) {
    const outcome = await deps.webPush.send(sub, body);
    if (outcome === 'ok') sent++;
    else if (outcome === 'gone') {
      deps.repos.pushSubs.deleteByEndpoint(sub.endpoint);
      pruned++;
    } else failed++;
  }
  return { sent, failed, pruned };
}

export async function sendTest(deps: NotifyDeps): Promise<ApiResult> {
  return json(
    await sendToUser(deps, {
      title: 'Prep \u2014 test push',
      body: 'If you can read this, notifications are working on this device.',
      url: '/notify',
    }),
  );
}
