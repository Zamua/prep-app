// The dashboard payload the shell embeds and the masthead badge: what
// each derives from the same deck summaries, and the badge's bucket order.
import { describe, expect, it } from 'vitest';
import { workflowBadge, workflowRow } from '../../app/badge/badge.js';
import { displayLabel, displayStatus, isActionRequired, isInProgress, isTerminal } from '../../app/badge/status.js';
import { menuContext, overviewPayload } from '../../app/dashboard/overview.js';
import type { ActiveWorkflow } from '../../app/entities.js';
import { cell, H } from '../repos/setup.js';

describe('the overview payload', () => {
  it('sums the decks, names the display and withholds the account id', () => {
    const c = cell();
    const a = c.repos.decks.create('world-capitals', { displayName: 'World Capitals' });
    c.repos.questions.add(a, { type: 'short', prompt: 'q', answer: 'a' });
    const trivia = c.repos.decks.createTrivia('quiz', { topic: 't', intervalMinutes: 30 });
    const qid = c.repos.questions.add(trivia, { type: 'short', prompt: 'tq', answer: 'ta' });
    c.repos.trivia.appendCard(qid, trivia);

    const payload = overviewPayload(c.repos, c.repos.prefs.get());
    expect(payload['user']).toEqual({ display_name: 'Parity', is_anonymous: false });
    expect(payload).toMatchObject({ due: 1, total: 2, unsynced: null });
    expect(JSON.stringify(payload)).not.toContain('parity@example.com');
    const decks = payload['decks'] as Record<string, unknown>[];
    expect(decks.find((d) => d['slug'] === 'world-capitals')).toMatchObject({ display_name: 'World Capitals', deck_type: 'srs', trivia_stats: null });
    expect(decks.find((d) => d['slug'] === 'quiz')).toMatchObject({ trivia_stats: { total: 1, unanswered: 1, wrong: 0, mastered: 0 } });
  });

  it('falls back to the slug when a deck has no display name', () => {
    const c = cell();
    c.repos.decks.create('scratch');
    expect((overviewPayload(c.repos, c.repos.prefs.get())['decks'] as Record<string, unknown>[])[0]!['display_name']).toBe('scratch');
  });

  it('hands the menus the very summaries the list is built from', () => {
    const c = cell();
    c.repos.decks.create('scratch');
    expect(menuContext(c.repos.decks.listSummaries())['menu_decks']).toEqual(c.repos.decks.listSummaries());
  });
});

describe('the workflow buckets', () => {
  it('classifies every status into exactly one bucket', () => {
    for (const status of ['awaiting_apply', 'awaiting_feedback', 'done', 'FAILED', 'computing', '', 'asking_ai']) {
      const flags = [isActionRequired(status), isTerminal(status), isInProgress(status)].filter(Boolean);
      expect(flags, status).toHaveLength(1);
    }
  });

  it('shortens the status for the narrow popover', () => {
    expect(displayStatus('awaiting_apply')).toBe('review');
    expect(displayStatus('awaiting_feedback')).toBe('review plan');
    expect(displayStatus('COMPLETED')).toBe('done');
    expect(displayStatus('TERMINATED')).toBe('cancelled');
    expect(displayStatus('asking_ai')).toBe('asking AI');
    expect(displayStatus('')).toBe('starting');
    expect(displayStatus('computing')).toBe('computing');
  });

  it('labels a row by the deck a user would recognise', () => {
    const w = { deck_display_name: null as string | null, deck_name: null as string | null, workflow_type: 'transform' };
    expect(displayLabel(w)).toBe('reorganize');
    expect(displayLabel({ ...w, workflow_type: 'trivia_gen' })).toBe('trivia gen');
    expect(displayLabel({ ...w, deck_name: 'world-capitals' })).toBe('world-capitals');
    expect(displayLabel({ ...w, deck_name: 'world-capitals', deck_display_name: 'World Capitals' })).toBe('World Capitals');
  });
});

describe('the badge fragment', () => {
  function seedWorkflows(): ReturnType<typeof cell> {
    const c = cell();
    const register = (id: string, status: string, minutesAgo: number) => {
      c.clock.set(new Date(c.clock.now().getTime() - minutesAgo * 60_000));
      c.repos.jobs.register({ workflowId: id, workflowType: 'transform', deckId: null, deckName: null, urlPath: `/transform/${id}`, initialStatus: status });
      c.clock.set(new Date(c.clock.now().getTime() + minutesAgo * 60_000));
    };
    register('older-action', 'awaiting_apply', 20);
    register('running', 'computing', 10);
    register('newer-action', 'awaiting_feedback', 1);
    register('just-done', 'done', 5);
    c.repos.jobs.setTerminalAt('just-done', null);
    return c;
  }

  it('sorts awaiting-action first, then in-progress, then just-completed, newest first inside each', () => {
    const c = seedWorkflows();
    const rendered = workflowBadge(c.repos) as unknown as { page: string; context: { workflows: Record<string, unknown>[] } };
    expect(rendered.page).toBe('partials/workflow_badge.html');
    expect(rendered.context.workflows.map((w) => w['workflow_id'])).toEqual(['newer-action', 'older-action', 'running', 'just-done']);
  });

  it('drops a terminal row once it is past the window', () => {
    const c = seedWorkflows();
    c.clock.advance(2 * H);
    const rendered = workflowBadge(c.repos) as unknown as { context: { workflows: Record<string, unknown>[] } };
    expect(rendered.context.workflows.map((w) => w['workflow_id'])).not.toContain('just-done');
    expect(c.repos.jobs.get('just-done')).toBeNull();
  });

  it('carries the bucket flags the template reads off each row', () => {
    const w: ActiveWorkflow = {
      workflow_id: 'w',
      workflow_type: 'trivia_gen',
      deck_id: null,
      deck_name: 'quiz',
      deck_display_name: 'Quiz Night',
      status: 'awaiting_apply',
      started_at: '2026-03-14T15:00:00+00:00',
      terminal_at: null,
      url_path: '/x',
      notified_action_at: null,
      notified_terminal_at: null,
    };
    expect(workflowRow(w)).toMatchObject({
      is_action_required: true,
      is_terminal: false,
      is_in_progress: false,
      display_status: 'review',
      display_label: 'Quiz Night',
    });
  });
});
