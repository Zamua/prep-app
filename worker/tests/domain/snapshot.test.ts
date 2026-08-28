// The snapshot JSON is prompt bytes: a canned-reply stub keys on the message
// it is sent, so a reordered or extra key is a different message. Field
// order, the dropped-empty rule and the HTML escaping are all part of what
// the model is shown.
import { describe, expect, it } from 'vitest';
import { transformCard, transformDeck } from '../../domain/jobs/snapshot.js';
import { goJson } from '../../app/jobs/transform.js';

const FULL_CARD = `{
  "question_id": 7,
  "type": "mcq",
  "topic": "geo",
  "prompt": "Capital of \\u003cFrance\\u003e?",
  "choices": [
    "Paris",
    "Lyon"
  ],
  "answer": "Paris",
  "rubric": "- names the city",
  "skeleton": "def f():",
  "language": "python",
  "explanation": "It is Paris.",
  "answer_regex": "(?i)paris"
}`;

const BARE_CARD = `{
  "question_id": 8,
  "type": "short",
  "prompt": "Capital of Japan?",
  "answer": "Tokyo"
}`;

const DECKS = `[
  {
    "id": 1,
    "name": "capitals",
    "deck_type": "srs",
    "cards": []
  },
  {
    "id": 2,
    "name": "world-history",
    "deck_type": "trivia",
    "topic": "antiquity",
    "interval_minutes": 1440,
    "cards": [
      {
        "question_id": 8,
        "type": "short",
        "prompt": "Capital of Japan?",
        "answer": "Tokyo"
      }
    ]
  }
]`;

const bare = () =>
  transformCard({ question_id: 8, type: 'short', prompt: 'Capital of Japan?', answer: 'Tokyo', topic: '', choices: [], rubric: '', skeleton: '', language: '', explanation: '', answer_regex: '' });

describe('the transform snapshot', () => {
  it('a card with every column set keeps the struct field order', () => {
    const card = transformCard({
      question_id: 7,
      type: 'mcq',
      topic: 'geo',
      prompt: 'Capital of <France>?',
      choices: ['Paris', 'Lyon'],
      answer: 'Paris',
      rubric: '- names the city',
      skeleton: 'def f():',
      language: 'python',
      explanation: 'It is Paris.',
      answer_regex: '(?i)paris',
    });
    expect(goJson(card)).toBe(FULL_CARD);
  });

  it('an empty column is dropped, not sent as an empty string', () => {
    expect(goJson(bare())).toBe(BARE_CARD);
  });

  it("a deck's topic and interval precede its cards, and an empty deck is [] not null", () => {
    const decks = [transformDeck({ id: 1, name: 'capitals', deck_type: 'srs' }, []), transformDeck({ id: 2, name: 'world-history', deck_type: 'trivia', topic: 'antiquity', interval_minutes: 1440 }, [bare()])];
    expect(goJson(decks)).toBe(DECKS);
  });

  it('round-tripping through the persisted input does not reorder it', () => {
    const card = transformCard(JSON.parse(JSON.stringify(transformCard({ question_id: 7, type: 'mcq', topic: 'geo', prompt: 'p', answer: 'a' }))) as Record<string, unknown>);
    expect(Object.keys(card)).toEqual(['question_id', 'type', 'topic', 'prompt', 'answer']);
  });
});
