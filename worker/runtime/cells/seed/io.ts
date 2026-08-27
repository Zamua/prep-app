import { insertCards, q } from './cards.js';
import type { SeedContext } from './index.js';

/** The import, export and split screens (`prep/dev/parity_seed.py`
 * `profile_io`). One SRS deck whose cards carry every field an export
 * writes, and one trivia deck, because the export hub is the only page that
 * renders differently for the two. */
export async function profileIo(ctx: SeedContext): Promise<Record<string, unknown>> {
  const { repos, at } = ctx;
  const decks = repos.decks;

  const srs = decks.create('algorithms', { contextPrompt: 'Sorting, searching and complexity.', displayName: 'Algorithms' });
  const srsIds = insertCards(ctx, srs, [
    [
      'complexity',
      q('mcq', 'What is the average-case time of quicksort?', 'O(n log n)', {
        choices: ['O(n)', 'O(n log n)', 'O(n^2)', 'O(log n)'],
        topic: 'complexity',
      }),
      { due: at({ hours: -2 }), step: 2, last_review: at({ days: -2 }) },
    ],
    [
      'stability',
      q('short', 'Name a comparison sort that is stable.', 'Merge sort', { answer_regex: '(?i)merge', topic: 'sorting' }),
      { due: at({ hours: -1 }), step: 1, last_review: at({ days: -1 }) },
    ],
    [
      'binary',
      q(
        'code',
        'Return the index of `needle` in the sorted list `xs`, or -1.',
        'def find(xs, needle):\n    lo, hi = 0, len(xs) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if xs[mid] == needle:\n            return mid\n        if xs[mid] < needle:\n            lo = mid + 1\n        else:\n            hi = mid - 1\n    return -1\n',
        {
          language: 'python',
          skeleton: 'def find(xs, needle):\n    ...\n',
          rubric: '- Halves the range each step\n- Returns -1 on a miss',
          topic: 'searching',
        },
      ),
      { due: at({ days: 2 }), step: 3, last_review: at({ days: -5 }) },
    ],
    [
      'invariant',
      q('short', 'What does a loop invariant have to hold at?', 'Before the loop, after every iteration, and after the loop.', { topic: 'proofs' }),
      { due: at({ days: 4 }), step: 4, last_review: at({ days: -8 }) },
    ],
  ]);
  repos.reviews.importReview(srsIds['complexity']!, at({ days: -2 }), 'right', 'O(n log n)', null);
  repos.reviews.importReview(srsIds['stability']!, at({ days: -1 }), 'wrong', 'Heap sort', null);

  const trivia = decks.createTrivia('database-trivia', {
    topic: 'Storage engines, transactions and query planning.',
    intervalMinutes: 1440,
    displayName: 'Database Trivia',
  });
  const triviaIds: Record<string, number> = {};
  for (const [key, prompt, answer, regex] of [
    ['isolation', 'Which isolation level allows phantom reads?', 'Repeatable read', '(?i)repeatable'],
    ['index', 'What structure does a clustered index store the rows in?', 'The index itself', '(?i)index'],
    ['wal', 'What does a write-ahead log let a database skip on commit?', 'Flushing the data pages', '(?i)flush'],
  ] as const) {
    const qid = repos.questions.add(trivia, q('short', prompt, answer, { answer_regex: regex }));
    repos.trivia.appendCard(qid, trivia);
    triviaIds[key] = qid;
  }
  repos.trivia.markAnswered(triviaIds['isolation']!, true);

  return {
    decks: {
      srs: { id: srs, slug: 'algorithms', display: 'Algorithms' },
      trivia: { id: trivia, slug: 'database-trivia', display: 'Database Trivia' },
    },
    questions: { srs: srsIds, trivia: triviaIds },
  };
}
