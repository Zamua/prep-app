import { beforeEach, describe, expect, it } from 'vitest';
import worker from '../runtime/worker';
import { compose, composeWith } from '../runtime/compose';
import type { Env } from '../runtime/env';
import { HmacSigner, mintCookie, resolveCookieSecret } from '../runtime/adapters/anonCookie';
import { WebCryptoHasher } from '../runtime/adapters/hash';
import { NoIdentityProvider } from '../runtime/adapters/fakeIdentity';
import { assembleToken, maskToken } from '../domain/pat';
import type { Signer } from '../app/ports';
import { FakeDirectory, FakeUserCells } from './fakes/cells';
import { fakeEnv, namespaceOf, req, spyRenderer } from './helpers';

const PARITY_NOW = 1773500400;
const ANON = 'anon:' + 'ab'.repeat(16);
const OWNER = 'user_2token';
const AT = '2026-03-14T15:00:00+00:00';

let env: Env;
let cells: FakeUserCells;
let directory: FakeDirectory;
let signer: Signer;

beforeEach(async () => {
  // One set of cells behind both the namespace the worker routes through and
  // the handle the test seeds with, or the two halves never meet.
  env = fakeEnv({ USER: namespaceOf((name) => cells.entry(name).cell) });
  directory = new FakeDirectory();
  cells = new FakeUserCells(env);
  composeWith(env, { renderer: spyRenderer(), identity: new NoIdentityProvider(), directory, userCells: cells });
  signer = new HmacSigner((await resolveCookieSecret({ PREP_KEY_ENCRYPTION_SECRET: '11'.repeat(32) }))!);
});

const withCookie = async (id: string, path = '/api/offline/snapshot') =>
  worker.fetch(req(path, { headers: { accept: 'application/json', cookie: `prep_anon=${await mintCookie(signer, id, PARITY_NOW)}` } }), env);

describe('a cookie whose account is gone', () => {
  it('is refused by the cell and cleared by the response hook', async () => {
    // Never upserted, so the cell holds no profile row for this id.
    const res = await withCookie(ANON);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ detail: 'not authenticated' });
    expect(res.headers.get('set-cookie')).toMatch(/^prep_anon=""; expires=/);
    expect(res.headers.get('x-prep-anon-cookie')).toBeNull();
  });

  it('is refused when the row exists but is no longer flagged anonymous', async () => {
    // A cleared flag must not become an unrestricted session for the holder.
    await cells.cell(ANON).upsert(ANON, {}, AT);
    const res = await withCookie(ANON);
    expect(res.status).toBe(401);
    expect(res.headers.get('set-cookie')).toMatch(/^prep_anon=""/);
  });

  it('is honoured while the row is still anonymous', async () => {
    const storage = cells.entry(ANON).storage;
    await cells.cell(ANON).upsert(ANON, {}, AT);
    storage.sql.exec('UPDATE profile SET is_anonymous = 1');
    const res = await withCookie(ANON);
    expect(res.status).not.toBe(401);
    expect(res.headers.get('set-cookie')).toBeNull();
  });

  it('is cleared when the cell has been tombstoned', async () => {
    const storage = cells.entry(ANON).storage;
    await cells.cell(ANON).upsert(ANON, {}, AT);
    storage.sql.exec('UPDATE profile SET is_anonymous = 1');
    await cells.cell(ANON).destroy('merged', AT);
    const res = await withCookie(ANON);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ detail: 'not authenticated' });
    expect(res.headers.get('set-cookie')).toMatch(/^prep_anon=""/);
  });
});

describe('a token whose hash is not stored', () => {
  const bearer = async (token: string, path = '/api/v1/decks') =>
    worker.fetch(req(path, { headers: { authorization: `Bearer ${token}`, accept: 'application/json' } }), env);

  it('is refused by the cell that owns it', async () => {
    await cells.cell(OWNER).upsert(OWNER, {}, AT);
    const token = assembleToken(OWNER, new Uint8Array(32).fill(4));
    const res = await bearer(token);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ detail: 'invalid or revoked token' });
  });

  it('is accepted once the row is there, and stamps last_used_at', async () => {
    await cells.cell(OWNER).upsert(OWNER, {}, AT);
    const token = assembleToken(OWNER, new Uint8Array(32).fill(5));
    const hash = await new WebCryptoHasher().sha256Hex(token);
    const repos = compose(env).userRepos(cells.entry(OWNER).storage, { now: () => new Date(AT) });
    repos.tokens.insert(hash, maskToken(token), 'test');
    expect(repos.tokens.list()[0]!.last_used_at).toBeNull();
    const res = await bearer(token);
    expect(res.status).not.toBe(401);
    expect(repos.tokens.list()[0]!.last_used_at).not.toBeNull();
  });

  it('is refused when the token names a cell with no profile at all', async () => {
    const token = assembleToken('user_never_seen', new Uint8Array(32).fill(6));
    const res = await bearer(token);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ detail: 'invalid or revoked token' });
  });

  it('reads as unknown once its cell is tombstoned', async () => {
    await cells.cell(OWNER).upsert(OWNER, {}, AT);
    const token = assembleToken(OWNER, new Uint8Array(32).fill(5));
    const repos = compose(env).userRepos(cells.entry(OWNER).storage, { now: () => new Date(AT) });
    repos.tokens.insert(await new WebCryptoHasher().sha256Hex(token), maskToken(token), 'test');
    await cells.cell(OWNER).destroy('deleted', AT);
    const res = await bearer(token);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ detail: 'invalid or revoked token' });
  });
});
