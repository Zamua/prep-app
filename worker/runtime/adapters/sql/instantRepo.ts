// The instant deck write: the account when minting, the deck and every card
// in one transaction, so a half-made deck cannot outlive the request.
import type { Clock, InstantRepo, Random } from '../../../app/ports.js';
import { SLUG_ALPHABET, SLUG_LENGTH, type InstantCard, type InstantDeckResult } from '../../../app/entities.js';
import { refuseOverRowCap } from './caps.js';
import { Db, type CellStorage } from './storage.js';
import { isoNow } from './time.js';

// A slug is 40 bits over a per-user namespace; the bound only trips when
// the generator is broken.
const MAX_SLUG_ATTEMPTS = 100;

export class SqlInstantRepo implements InstantRepo {
  private readonly db: Db;

  constructor(
    private readonly storage: CellStorage,
    private readonly clock: Clock,
    private readonly random: Random,
  ) {
    this.db = new Db(storage.sql);
  }

  private freeSlug(): string {
    const alphabet = Array.from(SLUG_ALPHABET);
    for (let i = 0; i < MAX_SLUG_ATTEMPTS; i++) {
      let candidate = '';
      for (let k = 0; k < SLUG_LENGTH; k++) candidate += this.random.choice(alphabet);
      if (this.db.first('SELECT 1 AS one FROM decks WHERE name = ?', candidate) === null) return candidate;
    }
    throw new Error(`no free deck slug after ${MAX_SLUG_ATTEMPTS} attempts`);
  }

  createInstantDeck(displayName: string, cards: readonly InstantCard[], mint: { id: string; displayName: string } | null): InstantDeckResult {
    const ts = isoNow(this.clock);
    return this.storage.transactionSync(() => {
      if (mint) {
        this.db.run(
          'INSERT INTO profile (id, display_name, email, created_at, last_seen_at, is_anonymous) VALUES (?, ?, NULL, ?, ?, 1)',
          mint.id,
          mint.displayName,
          ts,
          ts,
        );
      } else {
        refuseOverRowCap(this.db, { newDecks: 1, newQuestions: cards.length });
      }
      const slug = this.freeSlug();
      const deckId = this.db.insert('INSERT INTO decks (name, display_name, created_at) VALUES (?, ?, ?)', slug, displayName, ts);
      for (const card of cards) {
        const qid = this.db.insert(
          `INSERT INTO questions (deck_id, type, prompt, answer, answer_regex, created_at) VALUES (?, 'short', ?, ?, ?, ?)`,
          deckId,
          card.prompt,
          card.answer,
          card.answer_regex,
          ts,
        );
        this.db.run('INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)', qid, ts);
      }
      return { slug, deck_id: deckId };
    });
  }
}
