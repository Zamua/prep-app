// The offline snapshot reads and the per-item sync writes, transcribed from
// prep/offline/repo.py. Each write is one item's effect in one transaction:
// the domain write and its ledger row commit together or not at all.
import type { CardRepo, Clock, DeckRepo, OfflineRepo } from '../../../app/ports.js';
import { DeckNameTaken, SyncItemRejected } from '../../../app/ports.js';
import type { ReviewResult, SnapshotCard, SnapshotDeck } from '../../../app/entities.js';
import { scheduleReview, stepForStability, type Fuzz, type FsrsStateValue } from '../../../domain/fsrs/index.js';
import { INBOX_DECK_NAME, MAX_DECK_SLUG_ATTEMPTS, slugForDeckName } from '../../../domain/offline/slug.js';
import { parseIso } from '../../../domain/py.js';
import { refuseOverRowCap } from './caps.js';
import { decodeChoices } from './questionRepo.js';
import { Db, type CellStorage } from './storage.js';
import { isoNow, isoUtc } from './time.js';

export class SqlOfflineRepo implements OfflineRepo {
  private readonly db: Db;

  constructor(
    private readonly storage: CellStorage,
    private readonly clock: Clock,
    private readonly decks: DeckRepo,
    private readonly cards: CardRepo,
    private readonly fuzz: Fuzz,
  ) {
    this.db = new Db(storage.sql);
  }

  snapshotDecks(): SnapshotDeck[] {
    return this.db
      .all(
        `SELECT d.id, d.name, d.display_name, d.pinned_at, COUNT(q.id) AS total
           FROM decks d LEFT JOIN questions q ON q.deck_id = d.id
          WHERE COALESCE(d.deck_type, 'srs') = 'srs'
          GROUP BY d.id
          ORDER BY (d.pinned_at IS NULL), d.pinned_at DESC, COALESCE(d.display_name, d.name)`,
      )
      .map((r) => ({
        id: Number(r['id']),
        name: String(r['name']),
        display_name: (r['display_name'] as string | null) ?? null,
        pinned_at: (r['pinned_at'] as string | null) ?? null,
        total: Number(r['total'] ?? 0),
      }));
  }

  snapshotCards(): SnapshotCard[] {
    return this.db
      .all(
        `SELECT q.id AS question_id, q.deck_id, q.type, q.prompt, q.choices, q.answer, q.answer_regex, q.rubric,
                q.skeleton, q.explanation, cards.next_due, cards.stability
           FROM questions q JOIN decks d ON d.id = q.deck_id JOIN cards ON cards.question_id = q.id
          WHERE COALESCE(q.suspended, 0) = 0 AND COALESCE(d.deck_type, 'srs') = 'srs'
          ORDER BY q.id`,
      )
      .map((r) => ({
        question_id: Number(r['question_id']),
        deck_id: Number(r['deck_id']),
        type: String(r['type']),
        prompt: String(r['prompt']),
        choices: decodeChoices(r['choices']),
        answer: String(r['answer']),
        answer_regex: (r['answer_regex'] as string | null) ?? null,
        rubric: (r['rubric'] as string | null) ?? null,
        skeleton: (r['skeleton'] as string | null) ?? null,
        explanation: (r['explanation'] as string | null) ?? null,
        step: stepForStability(r['stability'] == null ? null : Number(r['stability'])),
        next_due: String(r['next_due']),
      }));
  }

  resolveCardClientId(cardClientId: string): number | null {
    const row = this.db.first<{ question_id: number }>(
      `SELECT question_id FROM offline_sync_idempotency WHERE client_id = ? AND kind = 'card' AND status = 'created'`,
      cardClientId,
    );
    return row ? Number(row.question_id) : null;
  }

  findSrsDeckByLabel(label: string): number | null {
    const row = this.db.first<{ id: number }>(
      `SELECT id FROM decks WHERE COALESCE(deck_type, 'srs') = 'srs' AND COALESCE(display_name, name) = ? ORDER BY id LIMIT 1`,
      label,
    );
    return row ? Number(row.id) : null;
  }

  findSrsInbox(): { id: number } | { taken: boolean } {
    const row = this.db.first<{ id: number }>(`SELECT id FROM decks WHERE name = ? AND deck_type = 'srs'`, INBOX_DECK_NAME);
    if (row) return { id: Number(row.id) };
    return { taken: this.db.first('SELECT 1 AS one FROM decks WHERE name = ?', INBOX_DECK_NAME) !== null };
  }

  /** The inbox for deck-less cards; a non-SRS deck holding the name yields a suffixed inbox. */
  resolveSrsInbox(): number {
    const found = this.findSrsInbox();
    if ('id' in found) return found.id;
    return this.decks.getOrCreate(found.taken ? `${INBOX_DECK_NAME}-offline` : INBOX_DECK_NAME);
  }

  /** The SRS deck carrying `deckName` as its label, created past taken slugs; the inbox when the slug space is exhausted. */
  resolveNamedSrsDeck(deckName: string): number {
    const found = this.findSrsDeckByLabel(deckName);
    if (found !== null) return found;
    const base = slugForDeckName(deckName);
    for (let n = 1; n <= MAX_DECK_SLUG_ATTEMPTS; n++) {
      const slug = n === 1 ? base : `${base}-${n}`;
      if (this.decks.findId(slug) !== null) {
        const again = this.findSrsDeckByLabel(deckName);
        if (again !== null) return again;
        continue;
      }
      try {
        return this.decks.create(slug, { displayName: deckName });
      } catch (e) {
        if (!(e instanceof DeckNameTaken)) throw e;
        const again = this.findSrsDeckByLabel(deckName);
        if (again !== null) return again;
      }
    }
    return this.resolveSrsInbox();
  }

  createCard(clientId: string, deckId: number, prompt: string, answer: string, answerRegex: string | null): number {
    const ts = isoNow(this.clock);
    return this.storage.transactionSync(() => {
      const deck = this.db.first('SELECT id FROM decks WHERE id = ? AND COALESCE(deck_type, \'srs\') = \'srs\'', deckId);
      if (!deck) throw new SyncItemRejected('unknown deck_id');
      refuseOverRowCap(this.db, { newQuestions: 1 });
      const qid = this.db.insert(
        `INSERT INTO questions (deck_id, type, prompt, answer, answer_regex, created_at) VALUES (?, 'short', ?, ?, ?, ?)`,
        deckId,
        prompt,
        answer,
        answerRegex,
        ts,
      );
      this.db.run('INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)', qid, ts);
      this.db.run(
        `INSERT INTO offline_sync_idempotency (client_id, kind, status, question_id, created_at) VALUES (?, 'card', 'created', ?, ?)`,
        clientId,
        qid,
        ts,
      );
      return qid;
    });
  }

  applyReview(clientId: string, questionId: number, verdict: ReviewResult, userAnswer: string, reviewedAt: Date, notes: string): string {
    return this.storage.transactionSync(() => {
      const card = this.cards.srsState(questionId);
      if (!card) throw new SyncItemRejected('unknown question_id');
      const lastReview = card.last_review ? parseIso(card.last_review) : null;
      const reviewedIso = isoUtc(reviewedAt);
      let status: string;
      if (lastReview !== null && reviewedAt.getTime() <= lastReview.getTime()) {
        status = 'logged_no_reschedule';
        this.db.run(
          'INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) VALUES (?, ?, ?, ?, ?)',
          questionId,
          reviewedIso,
          verdict,
          userAnswer,
          notes,
        );
      } else {
        status = 'applied';
        const scheduled = scheduleReview(
          { stability: card.stability, difficulty: card.difficulty, fsrsState: (card.fsrs_state || 1) as FsrsStateValue, lastReview },
          verdict,
          reviewedAt,
          { desiredRetention: this.cards.effectiveRetention(questionId), fuzz: this.fuzz },
        );
        this.db.run(
          'INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) VALUES (?, ?, ?, ?, ?)',
          questionId,
          reviewedIso,
          verdict,
          userAnswer,
          notes,
        );
        this.cards.writeScheduled(questionId, scheduled, reviewedIso);
      }
      this.db.run(
        `INSERT INTO offline_sync_idempotency (client_id, kind, status, question_id, created_at) VALUES (?, 'review', ?, ?, ?)`,
        clientId,
        status,
        questionId,
        isoNow(this.clock),
      );
      return status;
    });
  }
}
