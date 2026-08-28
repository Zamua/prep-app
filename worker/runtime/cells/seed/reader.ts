import { maskToken } from '../../../domain/pat.js';
import { capitalsCards, insertCards, q } from './cards.js';
import { DEVICE_LABEL, type SeedContext } from './index.js';

export async function profileReader(ctx: SeedContext): Promise<Record<string, unknown>> {
  const { repos, at } = ctx;
  const decks = repos.decks;

  const a = decks.create('world-capitals', { contextPrompt: 'Capital cities of the world, one card per country.', displayName: 'World Capitals' });
  const aIds = insertCards(ctx, a, capitalsCards(at));
  repos.reviews.importReview(aIds['mcq']!, at({ days: -2 }), 'right', 'Canberra', null);
  repos.reviews.importReview(aIds['mcq']!, at({ days: -6 }), 'wrong', 'Sydney', null);
  repos.reviews.importReview(aIds['short_regex']!, at({ days: -1 }), 'right', 'Nairobi', null);
  repos.reviews.importReview(aIds['code']!, at({ days: -5 }), 'right', 'table.get(code)', null);

  const b = decks.create('distributed-systems', { contextPrompt: 'Consensus, replication and failure detection.', displayName: 'Distributed Systems' });
  const bIds = insertCards(ctx, b, [
    [
      'raft',
      q('short', 'In Raft, what does a follower do when its election timeout elapses?', 'It becomes a candidate, increments its term and requests votes.', {
        topic: 'consensus',
      }),
      { due: at({ minutes: -30 }), step: 1, last_review: at({ days: -1 }) },
    ],
    [
      'quorum',
      q('mcq', 'With N=5 replicas, the smallest write quorum that still overlaps every read quorum of 3 is:', '3', {
        choices: ['2', '3', '4', '5'],
        topic: 'replication',
      }),
      { due: at({ days: 1 }) },
    ],
    [
      'phi',
      q('short', 'What does a phi-accrual failure detector output?', 'A suspicion level that grows with silence, not a boolean.', { topic: 'failure-detection' }),
      { due: at({ days: 3 }), step: 2, last_review: at({ days: -4 }) },
    ],
  ]);
  decks.setPinned(b, true);
  repos.pins.pinnedAt(b, at({ days: -1 }));

  const e = decks.create('scratch', { contextPrompt: null, displayName: 'Scratch' });

  const t = decks.createTrivia('world-history', { topic: 'World history from antiquity to 1900.', intervalMinutes: 1440, displayName: 'World History Trivia' });
  const tIds: Record<string, number> = {};
  for (const [key, prompt, answer, regex, explanation] of [
    ['rome', "Which empire's western half fell in 476?", 'The Roman Empire', '(?i)rom', 'Odoacer deposed Romulus Augustulus in 476.'],
    ['print', 'Who introduced movable-type printing to Europe around 1450?', 'Johannes Gutenberg', '(?i)gutenberg', 'The Gutenberg Bible followed in the mid 1450s.'],
    ['magna', 'In which year was Magna Carta sealed?', '1215', '1215', 'At Runnymede, by King John.'],
  ] as const) {
    const qid = repos.questions.add(t, q('short', prompt, answer, { answer_regex: regex, explanation }));
    repos.trivia.appendCard(qid, t);
    tIds[key] = qid;
  }
  repos.trivia.markAnswered(tIds['rome']!, true);

  const active = await repos.sessions.create(a, DEVICE_LABEL);
  repos.pins.session(active, at({ minutes: -20 }), at({ minutes: -25 }));
  const snoozed = await repos.sessions.create(b, DEVICE_LABEL);
  repos.pins.session(snoozed, at({ hours: -6 }), at({ hours: -6, minutes: -10 }));
  repos.sessions.snooze(snoozed, at({ hours: 3 }));

  const n1 = repos.notify.append({
    title: '3 cards due in World Capitals',
    body: 'Canberra, Nairobi and two more are waiting.',
    url: '/study/world-capitals',
    source: 'digest',
  });
  repos.pins.notificationSentAt(n1, at({ hours: -3 }));
  const n2 = repos.notify.append({
    title: 'Distributed Systems is ready',
    body: 'One card came due while you were away.',
    url: '/study/distributed-systems',
    source: 'when-ready',
  });
  repos.pins.notificationSentAt(n2, at({ days: -1 }));

  // A PAT with a known plaintext, so its masked prefix is stable.
  const plaintext = 'prep_pat_SeedCliToken000000000000000000000000';
  const token = repos.tokens.insert(await ctx.hasher.sha256Hex(plaintext), maskToken(plaintext), 'Seed CLI');
  repos.pins.tokenCreatedAt(token.id, at({ days: -3 }));

  const wid = 'transform-world-capitals-seed01';
  repos.jobs.register({ workflowId: wid, workflowType: 'transform', deckId: a, deckName: 'world-capitals', urlPath: `/transform/${wid}`, initialStatus: 'computing' });
  repos.pins.workflowStartedAt(wid, at({ minutes: -5 }));

  return {
    decks: {
      srs_a: { id: a, slug: 'world-capitals', display: 'World Capitals' },
      srs_b: { id: b, slug: 'distributed-systems', display: 'Distributed Systems' },
      empty: { id: e, slug: 'scratch', display: 'Scratch' },
      trivia: { id: t, slug: 'world-history', display: 'World History Trivia' },
    },
    questions: { srs_a: aIds, srs_b: bIds, trivia: tIds },
    sessions: { active, snoozed },
    notifications: [n1, n2],
    api_tokens: [token.id],
    workflows: { transform: wid },
  };
}
