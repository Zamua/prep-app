import { beforeEach, describe, expect, it } from 'vitest';
import worker from '../runtime/worker';
import { composeWith } from '../runtime/compose';
import type { Env } from '../runtime/env';
import { displayName, primaryEmail, profilePic } from '../runtime/webhooks';
import { signSvix } from '../runtime/adapters/svix';
import { NoIdentityProvider } from '../runtime/adapters/fakeIdentity';
import { FakeDirectory, FakeUserCells } from './fakes/cells';
import { cellNamespace, fakeEnv, req } from './helpers';
import { UserCell } from '../runtime/cells/UserCell';

const SECRET = 'whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw';
const ID = 'msg_2abc';
const TS = String(Math.floor(new Date('2026-03-14T15:00:00Z').getTime() / 1000));
const USER = 'user_2clerk';

const CLERK_USER = {
  id: USER,
  first_name: 'Ada',
  last_name: 'Lovelace',
  username: 'ada',
  image_url: 'https://img.example.test/ada.png',
  primary_email_address_id: 'idn_2',
  email_addresses: [
    { id: 'idn_1', email_address: 'old@example.test' },
    { id: 'idn_2', email_address: 'ada@example.test' },
  ],
};

let env: Env;
let directory: FakeDirectory;
let cells: FakeUserCells;

beforeEach(() => {
  env = fakeEnv({ CLERK_WEBHOOK_SECRET: SECRET, USER: cellNamespace((state, e) => new UserCell(state, e), () => env) });
  directory = new FakeDirectory();
  cells = new FakeUserCells(env);
  composeWith(env, { identity: new NoIdentityProvider(), directory, userCells: cells });
});

async function post(body: unknown, opts: { secret?: string; id?: string; ts?: string; signature?: string } = {}): Promise<Response> {
  const raw = typeof body === 'string' ? body : JSON.stringify(body);
  const id = opts.id ?? ID;
  const ts = opts.ts ?? TS;
  const signature = opts.signature ?? (await signSvix(opts.secret ?? SECRET, id, ts, raw));
  return worker.fetch(
    req('/webhooks/clerk', { method: 'POST', body: raw, headers: { 'svix-id': id, 'svix-timestamp': ts, 'svix-signature': signature } }),
    env,
  );
}

describe('the payload readers', () => {
  it('take the primary address, then the first, then none', () => {
    expect(primaryEmail(CLERK_USER)).toBe('ada@example.test');
    expect(primaryEmail({ email_addresses: [{ id: 'x', email_address: 'first@example.test' }] })).toBe('first@example.test');
    expect(primaryEmail({})).toBeNull();
  });

  it('build the name from first plus last, then username, then the local part', () => {
    expect(displayName(CLERK_USER)).toBe('Ada Lovelace');
    expect(displayName({ first_name: 'Ada' })).toBe('Ada');
    expect(displayName({ username: 'ada' })).toBe('ada');
    expect(displayName({ email_addresses: [{ id: 'x', email_address: 'ada@example.test' }] })).toBe('ada');
    expect(displayName({})).toBeNull();
    expect(profilePic({ profile_image_url: 'https://a' })).toBe('https://a');
    expect(profilePic({})).toBeNull();
  });
});

describe('the receiver', () => {
  it('lands the profile row before the user has ever visited', async () => {
    const res = await post({ type: 'user.created', data: CLERK_USER });
    expect(res.status).toBe(200);
    expect(await res.text()).toBe('');
    const profile = (await cells.cell(USER).dump()).profile!;
    expect(profile['login']).toBe(USER);
    expect(profile['email']).toBe('ada@example.test');
    expect(profile['display_name']).toBe('Ada Lovelace');
    expect(profile['profile_pic_url']).toBe('https://img.example.test/ada.png');
    expect(await directory.lookup(USER)).toMatchObject({ id: USER, is_anonymous: false });
  });

  it('keeps the id block the directory handed out', async () => {
    await directory.register('someone_first', false, '2026-03-14T15:00:00+00:00');
    await post({ type: 'user.created', data: CLERK_USER });
    const idx = (await directory.lookup(USER))!.idx;
    expect(idx).toBeGreaterThan(0);
    const deckId = (await cells.cell(USER).dump()).tables['decks']?.length ?? 0;
    expect(deckId).toBe(0);
  });

  it('refreshes the mirror on user.updated', async () => {
    await post({ type: 'user.created', data: CLERK_USER });
    const renamed = { ...CLERK_USER, first_name: 'Augusta', last_name: '', username: null };
    await post({ type: 'user.updated', data: renamed });
    expect((await cells.cell(USER).dump()).profile!['display_name']).toBe('Augusta');
  });

  it('retires the whole cell on user.deleted', async () => {
    await post({ type: 'user.created', data: CLERK_USER });
    const res = await post({ type: 'user.deleted', data: { id: USER, deleted: true } });
    expect(res.status).toBe(200);
    expect((await cells.cell(USER).precheck()).tombstoned).toBe('deleted');
    expect(await directory.lookup(USER)).toBeNull();
    expect(await directory.tombstoneOf(USER)).toMatchObject({ reason: 'deleted' });
  });

  it('acknowledges an event it does not act on', async () => {
    const res = await post({ type: 'session.created', data: { id: 'sess_1' } });
    expect(res.status).toBe(200);
    expect(await directory.lookup('sess_1')).toBeNull();
  });

  it('refuses a bad signature, a stale timestamp and a foreign secret', async () => {
    expect((await post({ type: 'user.created', data: CLERK_USER }, { secret: 'whsec_' + Buffer.from('x'.repeat(32)).toString('base64') })).status).toBe(400);
    expect((await post({ type: 'user.created', data: CLERK_USER }, { signature: 'v1,nope' })).status).toBe(400);
    expect((await post({ type: 'user.created', data: CLERK_USER }, { ts: String(Number(TS) - 3600) })).status).toBe(400);
    // A changed body against a signature over the original is the same refusal.
    const signature = await signSvix(SECRET, ID, TS, '{}');
    expect((await post({ type: 'user.created', data: CLERK_USER }, { signature })).status).toBe(400);
  });

  it('refuses a malformed payload with 422, which svix does not retry', async () => {
    for (const body of ['not json', {}, { type: 'user.created' }, { data: CLERK_USER }, { type: 'user.created', data: {} }]) {
      const res = await post(body);
      expect(res.status, JSON.stringify(body)).toBe(422);
    }
    expect((await post({ type: 'user.created', data: { first_name: 'No Id' } })).status).toBe(422);
    expect((await post({ type: 'user.deleted', data: { deleted: true } })).status).toBe(422);
  });

  it('answers 503 rather than dropping events when no secret is configured', async () => {
    const bare = fakeEnv({ CLERK_WEBHOOK_SECRET: undefined });
    composeWith(bare, { identity: new NoIdentityProvider(), directory, userCells: cells });
    const raw = JSON.stringify({ type: 'user.created', data: CLERK_USER });
    const res = await worker.fetch(
      req('/webhooks/clerk', { method: 'POST', body: raw, headers: { 'svix-id': ID, 'svix-timestamp': TS, 'svix-signature': 'v1,x' } }),
      bare,
    );
    expect(res.status).toBe(503);
  });

  it('is a POST-only route', async () => {
    expect((await worker.fetch(req('/webhooks/clerk'), env)).status).toBe(405);
  });
});
