import { describe, expect, it } from 'vitest';
import { harness, seeded, USER } from './setup.js';

const ANON = 'anon:' + 'ab'.repeat(16);

interface Overview {
  decks: { slug: string; pinned: boolean; due: number; total: number; trivia_stats: unknown }[];
  due: number;
  total: number;
  nextDueMinutes: number | null;
}

describe('GET /', () => {
  it('lists the reader decks, pinned first, with what each has due', async () => {
    const h = await seeded('reader');
    const res = await h.get('/');
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('text/html; charset=utf-8');
    expect(h.rendered()?.template).toBe('index.html');
    const ctx = h.rendered()!.context;
    const overview = ctx['dashboard_overview'] as Overview;
    expect(overview).toMatchObject({ due: 4, total: 12, nextDueMinutes: 1440 });
    const decks = [
      ['distributed-systems', true, 1, 3],
      ['scratch', false, 0, 0],
      ['world-capitals', false, 3, 6],
      ['world-history', false, 0, 3],
    ];
    expect(overview.decks.map((d) => [d.slug, d.pinned, d.due, d.total])).toEqual(decks);
    expect(overview.decks.at(-1)?.trivia_stats).toEqual({ mastered: 1, total: 3, unanswered: 2, wrong: 0 });
    expect((ctx['menu_decks'] as { name: string }[]).map((d) => d.name)).toEqual(decks.map(([slug]) => slug));
  });

  it('offers the running session to continue and the snoozed one apart', async () => {
    const h = await seeded('reader');
    await h.get('/');
    const ctx = h.rendered()!.context;
    expect(ctx['recent_sessions']).toMatchObject([{ deck_name: 'world-capitals', status: 'active', current_question_id: 1, device_label: 'iPhone' }]);
    expect(ctx['snoozed_sessions']).toMatchObject([{ deck_name: 'distributed-systems', kind: 'srs', snoozed_until: '2026-03-14T18:00:00+00:00' }]);
    expect(ctx['active_trivia_sessions']).toEqual([]);
  });

  it('shows an empty account nothing to do', async () => {
    const h = await seeded('empty');
    expect((await h.get('/')).status).toBe(200);
    expect(h.rendered()?.template).toBe('index.html');
    expect(h.rendered()?.context).toMatchObject({
      dashboard_overview: { decks: [], due: 0, total: 0, nextDueMinutes: null },
      menu_decks: [],
      recent_sessions: [],
      snoozed_sessions: [],
      active_trivia_sessions: [],
    });
  });

  // The instant-start product turns on this: an anonymous account's home is
  // the dashboard, not the landing page a visitor sees.
  it('renders for an anonymous account', async () => {
    const h = harness();
    await h.cell.createInstantDeck({
      displayName: 'Capitals',
      cards: [{ prompt: 'Capital of France?', answer: 'Paris', answer_regex: null }],
      mint: { id: ANON, displayName: 'Guest', idx: 7 },
      at: '2026-03-14T15:00:00+00:00',
    });
    const res = await h.get('/', { headers: { 'x-prep-subject': ANON, 'x-prep-kind': 'anon' } });
    expect(res.status).toBe(200);
    expect(h.rendered()?.template).toBe('index.html');
  });

  it('refuses an anonymous identity naming a cell that is not anonymous', async () => {
    const h = await seeded('reader');
    const res = await h.get('/', { headers: { 'x-prep-kind': 'anon' } });
    expect(res.status).toBe(401);
  });
});

describe('the context every signed-in page shares', () => {
  it.each(['/', '/deck/world-capitals', '/decks/new', '/settings/editor'])('%s names the account, its decks and its unseen notifications', async (path) => {
    const h = await seeded('reader');
    expect((await h.get(path)).status).toBe(200);
    expect(h.rendered()?.context).toMatchObject({
      user: { email: USER, display_name: 'Seed', is_anonymous: 0 },
      agent_available: true,
      auth_provider: 'tailscale',
      notif_unseen_count: 2,
      deck_display: {
        'distributed-systems': 'Distributed Systems',
        scratch: 'Scratch',
        'world-capitals': 'World Capitals',
        'world-history': 'World History Trivia',
      },
    });
  });
});
