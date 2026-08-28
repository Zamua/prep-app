import { describe, expect, it } from 'vitest';
import { DEFAULT_NOTIFICATION_PREFS } from '../../app/entities.js';
import { cell, D, H, PARITY_NOW, at } from './setup.js';

describe('NotifyRepo', () => {
  it('appends at second precision, lists newest first, counts and marks seen', () => {
    const { repos, clock, storage } = cell();
    clock.set(new Date(PARITY_NOW.getTime() + 500));
    const a = repos.notify.append({ title: 'A', body: 'b', url: '/a', source: 'digest' });
    clock.set(at(PARITY_NOW, H));
    const b = repos.notify.append({ title: 'B', body: 'b', url: '/b', source: 'when-ready' });
    expect(storage.rows('notifications_log')[0]?.['sent_at']).toBe('2026-03-14T15:00:00+00:00');
    expect(repos.notify.listRecent().map((n) => n.id)).toEqual([b, a]);
    expect(repos.notify.listRecent(1)).toHaveLength(1);
    expect(repos.notify.listRecent()[0]).toEqual({ id: b, sent_at: '2026-03-14T16:00:00+00:00', title: 'B', body: 'b', url: '/b', source: 'when-ready', seen_at: null });
    expect(repos.notify.countUnseen()).toBe(2);
    repos.notify.markAllSeen();
    expect(repos.notify.countUnseen()).toBe(0);
    expect(repos.notify.listRecent()[0]?.seen_at).toBe('2026-03-14T16:00:00+00:00');
  });
});

describe('PushSubRepo', () => {
  it('upserts by endpoint, lists, counts and prunes', () => {
    const { repos, clock, storage } = cell();
    repos.pushSubs.upsert('https://push/1', 'p1', 'a1');
    clock.set(at(PARITY_NOW, H));
    repos.pushSubs.upsert('https://push/1', 'p2', 'a2');
    repos.pushSubs.upsert('https://push/2', 'p', 'a');
    expect(repos.pushSubs.list()).toEqual([
      { endpoint: 'https://push/1', p256dh: 'p2', auth: 'a2' },
      { endpoint: 'https://push/2', p256dh: 'p', auth: 'a' },
    ]);
    expect(storage.rows('push_subscriptions')[0]).toMatchObject({ created_at: '2026-03-14T15:00:00+00:00', last_seen_at: '2026-03-14T16:00:00+00:00' });
    expect(repos.pushSubs.count()).toBe(2);
    repos.pushSubs.deleteByEndpoint('https://push/1');
    expect(repos.pushSubs.count()).toBe(1);
  });
});

describe('ByokRepo', () => {
  it('stores ciphertext per provider, replaces on re-store, never returns the blob as metadata', () => {
    const { repos, clock } = cell();
    const meta = repos.byok.store('anthropic-api', 'ct1', 'sk-ant-…x9zT');
    expect(meta).toEqual({ provider: 'anthropic-api', key_prefix: 'sk-ant-…x9zT', created_at: '2026-03-14T15:00:00+00:00', last_used_at: null });
    repos.byok.touchLastUsed('anthropic-api');
    expect(repos.byok.metadata('anthropic-api')?.last_used_at).toBe('2026-03-14T15:00:00+00:00');
    clock.set(at(PARITY_NOW, D));
    repos.byok.store('anthropic-api', 'ct2', 'sk-ant-…abcd');
    expect(repos.byok.getCiphertext('anthropic-api')).toBe('ct2');
    expect(repos.byok.metadata('anthropic-api')).toEqual({ provider: 'anthropic-api', key_prefix: 'sk-ant-…abcd', created_at: '2026-03-15T15:00:00+00:00', last_used_at: null });
    repos.byok.store('claude-subscription', 'old', 'x');
    expect(repos.byok.listProviders()).toEqual(['anthropic-api', 'claude-subscription']);
    expect(repos.byok.getCiphertext('openai-api')).toBeNull();
    expect(repos.byok.metadata('openai-api')).toBeNull();
    expect(repos.byok.delete('anthropic-api')).toBe(true);
    expect(repos.byok.delete('anthropic-api')).toBe(false);
  });
});

describe('TokenRepo', () => {
  it('stores the hash and mask, lists newest first, looks up by hash touching last_used_at', () => {
    const { repos, clock } = cell();
    const a = repos.tokens.insert('hash-a', 'prep_pat_Aa…x9zT', 'CLI');
    expect(a).toEqual({ id: 1, label: 'CLI', key_prefix: 'prep_pat_Aa…x9zT', created_at: '2026-03-14T15:00:00+00:00', last_used_at: null });
    clock.set(at(PARITY_NOW, H));
    const b = repos.tokens.insert('hash-b', 'prep_pat_Bb…0000', null);
    expect(repos.tokens.list().map((t) => t.id)).toEqual([b.id, a.id]);
    expect(repos.tokens.lookup('hash-a')).toEqual({ id: a.id });
    expect(repos.tokens.list().find((t) => t.id === a.id)?.last_used_at).toBe('2026-03-14T16:00:00+00:00');
    expect(repos.tokens.lookup('nope')).toBeNull();
    expect(() => repos.tokens.insert('hash-a', 'x', null)).toThrow(/UNIQUE/);
    expect(repos.tokens.delete(a.id)).toBe(true);
    expect(repos.tokens.delete(a.id)).toBe(false);
    expect(repos.tokens.lookup('hash-a')).toBeNull();
  });
});

describe('IdempotencyRepo: the three ledgers', () => {
  it('grading: a key replays its state and refuses a second insert', () => {
    const { repos } = cell();
    const state = { step: 1, next_due: '2026-03-14T15:10:00+00:00', interval_minutes: 10 };
    expect(repos.idempotency.findGrading('wf-1')).toBeNull();
    repos.idempotency.recordGrading('wf-1', 7, state);
    expect(repos.idempotency.findGrading('wf-1')).toEqual(state);
    expect(() => repos.idempotency.recordGrading('wf-1', 7, state)).toThrow(/UNIQUE|PRIMARY KEY/);
  });

  it('offline sync: keyed by client id, rejections never pinned', () => {
    const { repos } = cell();
    expect(repos.idempotency.findSync('c1')).toBeNull();
    repos.idempotency.recordSync('c1', 'card', 'created', 12);
    repos.idempotency.recordSync('c2', 'review', 'logged_no_reschedule', 12);
    expect(repos.idempotency.findSync('c1')).toEqual({ kind: 'card', status: 'created', question_id: 12 });
    expect(repos.idempotency.findSync('c2')).toEqual({ kind: 'review', status: 'logged_no_reschedule', question_id: 12 });
    expect(() => repos.idempotency.recordSync('c1', 'card', 'created', 13)).toThrow(/UNIQUE|PRIMARY KEY/);
  });

  it('question inserts: the <job>-insert-N key names the row it made', () => {
    const { repos } = cell();
    expect(repos.idempotency.findQuestion('job-insert-0')).toBeNull();
    repos.idempotency.recordQuestion('job-insert-0', 41);
    expect(repos.idempotency.findQuestion('job-insert-0')).toBe(41);
    expect(() => repos.idempotency.recordQuestion('job-insert-0', 42)).toThrow(/UNIQUE|PRIMARY KEY/);
  });
});

describe('PrefsRepo', () => {
  it('reads the profile as the Python user dict', () => {
    const { repos } = cell({ profile: false });
    expect(repos.prefs.get()).toBeNull();
    const p = repos.prefs.upsert('parity@example.com', { email: 'parity@example.com', displayName: 'Parity' });
    expect(p).toEqual({
      tailscale_login: 'parity@example.com',
      display_name: 'Parity',
      profile_pic_url: null,
      created_at: '2026-03-14T15:00:00+00:00',
      last_seen_at: '2026-03-14T15:00:00+00:00',
      notification_prefs: null,
      editor_input_mode: null,
      email: 'parity@example.com',
      active_byok_provider: null,
      desired_retention: null,
      is_anonymous: 0,
    });
    expect(Object.keys(p)).toEqual([
      'tailscale_login',
      'display_name',
      'profile_pic_url',
      'created_at',
      'last_seen_at',
      'notification_prefs',
      'editor_input_mode',
      'email',
      'active_byok_provider',
      'desired_retention',
      'is_anonymous',
    ]);
  });

  it('upsert bumps last_seen_at and keeps claims a later request omits; touch inserts nothing', () => {
    const { repos, clock } = cell({ profile: false });
    repos.prefs.touch();
    expect(repos.prefs.get()).toBeNull();
    repos.prefs.upsert('u', { email: 'u@x', displayName: 'U', profilePicUrl: 'pic' });
    clock.set(at(PARITY_NOW, H));
    const again = repos.prefs.upsert('u', {});
    expect(again).toMatchObject({ email: 'u@x', display_name: 'U', profile_pic_url: 'pic', created_at: '2026-03-14T15:00:00+00:00', last_seen_at: '2026-03-14T16:00:00+00:00' });
    clock.set(at(PARITY_NOW, 2 * H));
    repos.prefs.touch();
    expect(repos.prefs.get()?.last_seen_at).toBe('2026-03-14T17:00:00+00:00');
    expect(repos.prefs.upsert('u', { displayName: 'New' }).display_name).toBe('New');
  });

  it('notification prefs merge over the defaults and store as JSON', () => {
    const { repos, storage } = cell();
    expect(repos.prefs.getNotificationPrefs()).toEqual(DEFAULT_NOTIFICATION_PREFS);
    const prefs = repos.prefs.getNotificationPrefs();
    prefs.tz = 'Europe/Paris';
    prefs.mode = 'digest';
    repos.prefs.setNotificationPrefs(prefs);
    expect(storage.rows('profile')[0]?.['notification_prefs']).toBe(
      '{"mode":"digest","digest_hour":9,"tz":"Europe/Paris","threshold":3,"quiet_hours_enabled":false,"quiet_start_hour":22,"quiet_end_hour":8,"last_digest_date":null,"last_when_ready_at":null}',
    );
    expect(repos.prefs.getNotificationPrefs()).toEqual({ ...DEFAULT_NOTIFICATION_PREFS, tz: 'Europe/Paris', mode: 'digest' });
  });

  it('editor mode, active provider, retention, id_base, account rows', () => {
    const { repos } = cell();
    expect(repos.prefs.getEditorInputMode()).toBe('vanilla');
    repos.prefs.setEditorInputMode('vim');
    expect(repos.prefs.getEditorInputMode()).toBe('vim');
    expect(() => repos.prefs.setEditorInputMode('nano')).toThrow(RangeError);
    expect(repos.prefs.getActiveByokProvider()).toBeNull();
    repos.prefs.setActiveByokProvider('openai-api');
    expect(repos.prefs.getActiveByokProvider()).toBe('openai-api');
    repos.prefs.setActiveByokProvider(null);
    expect(repos.prefs.getActiveByokProvider()).toBeNull();
    expect(repos.prefs.getDesiredRetention()).toBeNull();
    repos.prefs.setDesiredRetention(0.92);
    expect(repos.prefs.getDesiredRetention()).toBe(0.92);
    expect(repos.prefs.getIdBase()).toBe(0);
    repos.prefs.setIdBase(7);
    expect(repos.prefs.getIdBase()).toBe(7);
    repos.decks.create('d');
    expect(repos.prefs.accountRows()).toEqual({ isAnonymous: false, decks: 1, questions: 0 });
  });

  it('createAnonymous writes the Guest row', () => {
    const { repos } = cell({ profile: false });
    const p = repos.prefs.createAnonymous('anon:' + 'ab'.repeat(16), 'Guest');
    expect(p).toMatchObject({ tailscale_login: 'anon:' + 'ab'.repeat(16), display_name: 'Guest', email: null, is_anonymous: 1 });
    expect(repos.prefs.accountRows()).toEqual({ isAnonymous: true, decks: 0, questions: 0 });
    expect(cell({ profile: false }).repos.prefs.accountRows().isAnonymous).toBeNull();
  });
});

describe('JobStatusRepo', () => {
  it('registers once, updates, stamps terminal and notified once, lists with the deck display name', () => {
    const { repos, clock } = cell();
    repos.decks.create('world-capitals', { displayName: 'World Capitals' });
    repos.jobs.register({ workflowId: 'w1', workflowType: 'transform', deckId: 1, deckName: 'world-capitals', urlPath: '/transform/w1' });
    repos.jobs.register({ workflowId: 'w1', workflowType: 'plan', deckId: null, deckName: null, urlPath: '/x' });
    expect(repos.jobs.get('w1')).toEqual({
      workflow_id: 'w1',
      workflow_type: 'transform',
      deck_id: 1,
      deck_name: 'world-capitals',
      deck_display_name: null,
      status: 'computing',
      started_at: '2026-03-14T15:00:00+00:00',
      terminal_at: null,
      url_path: '/transform/w1',
      notified_action_at: null,
      notified_terminal_at: null,
    });
    expect(repos.jobs.listForUser()[0]?.deck_display_name).toBe('World Capitals');
    repos.jobs.updateStatus('w1', 'awaiting_apply');
    repos.jobs.markNotified('w1', 'action');
    clock.set(at(PARITY_NOW, H));
    repos.jobs.markNotified('w1', 'action');
    expect(repos.jobs.get('w1')?.notified_action_at).toBe('2026-03-14T15:00:00+00:00');
    expect(() => repos.jobs.markNotified('w1', 'x' as 'action')).toThrow(RangeError);
    repos.jobs.updateStatus('w1', 'done');
    repos.jobs.setTerminalAt('w1');
    repos.jobs.setTerminalAt('w1', '2030-01-01T00:00:00+00:00');
    expect(repos.jobs.get('w1')?.terminal_at).toBe('2026-03-14T16:00:00+00:00');
    expect(repos.jobs.listNonTerminal()).toEqual([]);
    expect(repos.jobs.listForUser().map((w) => w.workflow_id)).toEqual(['w1']);
    clock.set(at(PARITY_NOW, H + 61_000));
    expect(repos.jobs.listForUser()).toEqual([]);
    expect(repos.jobs.cleanupStaleTerminal()).toBe(1);
    expect(repos.jobs.get('w1')).toBeNull();
  });

  it('orders newest first and prunes on the reconciler window', () => {
    const { repos, clock } = cell();
    repos.jobs.register({ workflowId: 'old', workflowType: 'plan', deckId: null, deckName: null, urlPath: '/a' });
    clock.set(at(PARITY_NOW, H));
    repos.jobs.register({ workflowId: 'new', workflowType: 'plan', deckId: null, deckName: null, urlPath: '/b' });
    expect(repos.jobs.listForUser().map((w) => w.workflow_id)).toEqual(['new', 'old']);
    expect(repos.jobs.listNonTerminal().map((w) => w.workflow_id)).toEqual(['old', 'new']);
    repos.jobs.setTerminalAt('old', '2026-03-13T00:00:00+00:00');
    expect(repos.jobs.pruneTerminalOlderThan()).toBe(1);
  });

  it('removes one progress row while its badge row stands, which reads as gone', () => {
    const { repos } = cell();
    repos.jobs.register({ workflowId: 'w1', workflowType: 'plan', deckId: null, deckName: null, urlPath: '/plan/w1' });
    repos.jobProgress.upsert({ workflowId: 'w1', transition: 1, status: 'planning', progress: {} });
    expect(repos.jobProgress.remove('w1')).toBe(true);
    expect(repos.jobProgress.remove('w1')).toBe(false);
    expect(repos.jobProgress.get('w1')).toBeNull();
    expect(repos.jobs.get('w1')).not.toBeNull();
  });
});
