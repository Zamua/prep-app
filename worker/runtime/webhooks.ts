// Clerk's `user.*` events, svix-signed. The canonical write path: someone
// signs up on the hosted UI, `user.created` arrives, and the profile row is
// there before their first request. `user.deleted` retires the whole cell
// through the same three-step deletion the merge and the reaper use.
import { destroyAccount } from '../app/auth/mergeSaga.js';
import { isoUtc } from '../domain/time.js';
import { clockFor, type Composition } from './compose.js';

export type ClerkUser = Record<string, unknown>;

/** Clerk sends `email_addresses: [{id, email_address}]` and a pointer. */
export function primaryEmail(data: ClerkUser): string | null {
  const list = (Array.isArray(data['email_addresses']) ? data['email_addresses'] : []) as Record<string, unknown>[];
  const pid = data['primary_email_address_id'];
  for (const addr of list) if (addr['id'] === pid) return (addr['email_address'] as string) || null;
  return list.length ? ((list[0]!['email_address'] as string) || null) : null;
}

/** First plus last, else the username, else the local part of the email. */
export function displayName(data: ClerkUser): string | null {
  const first = String(data['first_name'] ?? '').trim();
  const last = String(data['last_name'] ?? '').trim();
  const full = `${first} ${last}`.trim();
  if (full) return full;
  const username = data['username'];
  if (typeof username === 'string' && username) return username;
  const email = primaryEmail(data);
  return email ? (email.split('@')[0] ?? null) : null;
}

export function profilePic(data: ClerkUser): string | null {
  return (data['image_url'] as string) || (data['profile_image_url'] as string) || null;
}

/**
 * 200 on success, 400 on a bad signature, 422 on a malformed payload, 503
 * with no secret. Never 5xx for known-bad input: svix retries 5xx hard, and
 * a malformed payload will not fix itself.
 */
export async function clerkWebhook(request: Request, c: Composition): Promise<Response> {
  if (!c.webhooks) return Response.json({ detail: 'CLERK_WEBHOOK_SECRET not configured' }, { status: 503 });
  const body = await request.text();
  if (await c.webhooks.verify(request, body, clockFor(c, request).now())) {
    return Response.json({ detail: 'invalid signature' }, { status: 400 });
  }

  let payload: { type?: unknown; data?: unknown };
  try {
    payload = JSON.parse(body) as typeof payload;
  } catch {
    return Response.json({ detail: 'malformed payload (missing type/data)' }, { status: 422 });
  }
  const type = typeof payload.type === 'string' ? payload.type : '';
  const data = (payload.data ?? null) as ClerkUser | null;
  if (!type || !data || typeof data !== 'object' || Object.keys(data).length === 0) {
    return Response.json({ detail: 'malformed payload (missing type/data)' }, { status: 422 });
  }
  const id = typeof data['id'] === 'string' ? data['id'] : '';
  const at = isoUtc(clockFor(c, request).now());

  if (type === 'user.created' || type === 'user.updated') {
    if (!id) return Response.json({ detail: 'user payload missing id' }, { status: 422 });
    const { idx } = await c.directory.register(id, false, at);
    await c.userCells.cell(id).upsert(id, { email: primaryEmail(data), displayName: displayName(data), profilePicUrl: profilePic(data) }, at, idx);
  } else if (type === 'user.deleted') {
    if (!id) return Response.json({ detail: 'delete payload missing id' }, { status: 422 });
    await destroyAccount(id, 'deleted', { cells: c.userCells, jobs: c.jobCells, directory: c.directory, clock: clockFor(c, request) });
  }
  // Anything else is acknowledged so svix stops retrying it.
  return new Response('', { status: 200 });
}
