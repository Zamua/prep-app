// `questions` and the cards row an SRS question carries, transcribed from
// prep/decks/repo.py: QuestionRepo.
import type { Clock, QuestionRepo } from '../../../app/ports.js';
import { QuestionNotFound } from '../../../app/ports.js';
import type { DeckCard, NewQuestion, Question, QuestionType } from '../../../app/entities.js';
import { transformCard, type TransformCard } from '../../../domain/jobs/snapshot.js';
import { pyJsonDumps } from '../../../domain/py.js';
import { refuseOverRowCap } from './caps.js';
import { Db, type CellStorage, type Row } from './storage.js';
import { isoNow } from './time.js';

/** A JSON `choices` column decoded, or null when unreadable. */
export function decodeChoices(raw: unknown): string[] | null {
  if (typeof raw !== 'string') return (raw as string[] | null) ?? null;
  try {
    return JSON.parse(raw) as string[];
  } catch {
    return null;
  }
}

export function rowToQuestion(r: Row): Question {
  return {
    id: Number(r['id']),
    deck_id: Number(r['deck_id']),
    type: String(r['type']) as QuestionType,
    topic: (r['topic'] as string | null) ?? null,
    prompt: String(r['prompt']),
    choices: decodeChoices(r['choices']),
    answer: String(r['answer']),
    rubric: (r['rubric'] as string | null) ?? null,
    created_at: String(r['created_at']),
    suspended: Boolean(r['suspended'] ?? 0),
    skeleton: (r['skeleton'] as string | null) ?? null,
    language: (r['language'] as string | null) ?? null,
    explanation: (r['explanation'] as string | null) ?? null,
    answer_regex: (r['answer_regex'] as string | null) ?? null,
  };
}

/** The Go `cardForTransform` projection, column for column. */
const TRANSFORM_COLUMNS = `SELECT id AS question_id, type, COALESCE(topic, '') AS topic, prompt, choices,
                answer, COALESCE(rubric, '') AS rubric, COALESCE(skeleton, '') AS skeleton,
                COALESCE(language, '') AS language, COALESCE(explanation, '') AS explanation,
                COALESCE(answer_regex, '') AS answer_regex
           FROM questions`;

/** `choices` reaches the snapshot decoded, the way Go unmarshalled it. */
const rowFields = (r: Row): Record<string, unknown> => ({ ...r, choices: decodeChoices(r['choices']) ?? [] });

function rowToDeckCard(r: Row): DeckCard {
  return {
    id: Number(r['id']),
    type: String(r['type']) as QuestionType,
    topic: (r['topic'] as string | null) ?? null,
    prompt: String(r['prompt']),
    choices: decodeChoices(r['choices']),
    answer: String(r['answer']),
    rubric: (r['rubric'] as string | null) ?? null,
    suspended: Boolean(r['suspended'] ?? 0),
    skeleton: (r['skeleton'] as string | null) ?? null,
    language: (r['language'] as string | null) ?? null,
    answer_regex: (r['answer_regex'] as string | null) ?? null,
    step: Number(r['step'] || 0),
    next_due: (r['next_due'] as string | null) ?? null,
    last_review: (r['last_review'] as string | null) ?? null,
    rights: Number(r['rights'] || 0),
    attempts: Number(r['attempts'] || 0),
  };
}

/** The column forms of a `NewQuestion`: a `multi` answer list as JSON, a rubric list as bullets. */
function columnsOf(q: NewQuestion) {
  const answer = Array.isArray(q.answer) ? pyJsonDumps(q.answer) : q.answer;
  const rubric = Array.isArray(q.rubric) ? q.rubric.map((b) => `- ${b}`).join('\n') : (q.rubric ?? null);
  const isCode = q.type === 'code';
  return {
    answer,
    rubric,
    choices: q.choices && q.choices.length ? pyJsonDumps(q.choices) : null,
    skeleton: isCode && q.skeleton ? q.skeleton : null,
    language: isCode ? (q.language ?? null) : null,
  };
}

export class SqlQuestionRepo implements QuestionRepo {
  private readonly db: Db;

  constructor(
    private readonly storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  add(deckId: number, q: NewQuestion): number {
    const c = columnsOf(q);
    const ts = isoNow(this.clock);
    return this.storage.transactionSync(() => {
      refuseOverRowCap(this.db, { newQuestions: 1 });
      const qid = this.db.insert(
        `INSERT INTO questions
           (deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language, explanation, answer_regex)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        deckId,
        q.type,
        q.topic ?? null,
        q.prompt,
        c.choices,
        c.answer,
        c.rubric,
        ts,
        c.skeleton,
        c.language,
        q.explanation ?? null,
        q.answer_regex ?? null,
      );
      const deck = this.db.first<{ deck_type: string }>(`SELECT COALESCE(deck_type, 'srs') AS deck_type FROM decks WHERE id = ?`, deckId);
      if (deck && deck.deck_type === 'srs') {
        this.db.run('INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)', qid, ts);
      }
      return qid;
    });
  }

  update(qid: number, q: NewQuestion): void {
    const c = columnsOf(q);
    const n = this.db.run(
      `UPDATE questions
          SET type = ?, topic = ?, prompt = ?, choices = ?, answer = ?, rubric = ?, skeleton = ?, language = ?, answer_regex = ?
        WHERE id = ?`,
      q.type,
      q.topic ?? null,
      q.prompt,
      c.choices,
      c.answer,
      c.rubric,
      c.skeleton,
      c.language,
      q.answer_regex ?? null,
      qid,
    );
    if (n === 0) throw new QuestionNotFound(`question ${qid} not found for user`);
  }

  replace(qid: number, q: NewQuestion): void {
    const c = columnsOf(q);
    const n = this.db.run(
      `UPDATE questions
          SET type = ?, topic = ?, prompt = ?, choices = ?, answer = ?, rubric = ?, skeleton = ?, language = ?, explanation = ?, answer_regex = ?
        WHERE id = ?`,
      q.type,
      q.topic ?? null,
      q.prompt,
      c.choices,
      c.answer,
      c.rubric,
      c.skeleton,
      c.language,
      q.explanation ?? null,
      q.answer_regex ?? null,
      qid,
    );
    if (n === 0) throw new QuestionNotFound(`question ${qid} not found for user`);
  }

  setAnswerRegex(qid: number, regex: string | null): boolean {
    return this.db.run('UPDATE questions SET answer_regex = ? WHERE id = ?', regex, qid) > 0;
  }

  get(qid: number): Question | null {
    const row = this.db.first('SELECT q.*, cards.step, cards.next_due FROM questions q LEFT JOIN cards ON cards.question_id = q.id WHERE q.id = ?', qid);
    return row ? rowToQuestion(row) : null;
  }

  moveToDeck(questionIds: readonly number[], destDeckId: number): number {
    if (questionIds.length === 0) return 0;
    const dst = this.db.first('SELECT id FROM decks WHERE id = ?', destDeckId);
    if (!dst) return 0;
    const marks = questionIds.map(() => '?').join(',');
    return this.db.run(`UPDATE questions SET deck_id = ? WHERE id IN (${marks})`, destDeckId, ...questionIds);
  }

  listInDeck(deckId: number): DeckCard[] {
    return this.db
      .all(
        `SELECT q.id, q.type, q.topic, q.prompt, q.suspended,
                q.answer, q.choices, q.rubric, q.skeleton, q.language, q.answer_regex,
                cards.step, cards.next_due, cards.last_review,
                (SELECT COUNT(*) FROM reviews r WHERE r.question_id=q.id) AS attempts,
                (SELECT COUNT(*) FROM reviews r WHERE r.question_id=q.id AND r.result='right') AS rights
           FROM questions q
           LEFT JOIN cards ON cards.question_id = q.id
          WHERE q.deck_id = ?
          ORDER BY cards.next_due ASC, q.id ASC`,
        deckId,
      )
      .map(rowToDeckCard);
  }

  cardForTransform(qid: number): TransformCard | null {
    const row = this.db.first(`${TRANSFORM_COLUMNS} WHERE id = ?`, qid);
    return row ? transformCard(rowFields(row)) : null;
  }

  cardsForTransform(deckId: number): TransformCard[] {
    return this.db.all(`${TRANSFORM_COLUMNS} WHERE deck_id = ? AND COALESCE(suspended, 0) = 0 ORDER BY id`, deckId).map((r) => transformCard(rowFields(r)));
  }

  promptsInDeck(deckId: number): string[] {
    return this.db.all<{ prompt: string }>('SELECT prompt FROM questions WHERE deck_id = ?', deckId).map((r) => r.prompt);
  }

  findByPrompt(deckId: number, prompt: string): number | null {
    const row = this.db.first<{ id: number }>('SELECT id FROM questions WHERE deck_id = ? AND LOWER(TRIM(prompt)) = ? ORDER BY id LIMIT 1', deckId, prompt.trim().toLowerCase());
    return row ? Number(row.id) : null;
  }

  setSuspended(qid: number, suspended: boolean): void {
    this.db.run('UPDATE questions SET suspended = ? WHERE id = ?', suspended ? 1 : 0, qid);
  }

  delete(qid: number): boolean {
    return this.db.run('DELETE FROM questions WHERE id = ?', qid) > 0;
  }
}
