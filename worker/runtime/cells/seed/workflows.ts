import { insertCards, q, type SeedCard } from './cards.js';
import { DEVICE_LABEL, type SeedContext } from './index.js';

const BINARY_SEARCH_ANSWER =
  'def find(xs, target):\n    lo, hi = 0, len(xs) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if xs[mid] == target:\n            return mid\n        if xs[mid] < target:\n            lo = mid + 1\n        else:\n            hi = mid - 1\n    return -1\n';

/** Two SRS decks and a trivia deck for the phase-4 job flows, plus a study
 * session whose first due card is free text. Jobs themselves are started by
 * the flow, so no workflow rows are seeded. */
export async function profileWorkflows(ctx: SeedContext): Promise<Record<string, unknown>> {
  const { repos, at } = ctx;

  const a = repos.decks.create('algorithms', { contextPrompt: 'Sorting, searching and complexity analysis.', displayName: 'Algorithms' });
  const aCards: SeedCard[] = [
    ['complexity', q('short', 'What is the average-case time complexity of quicksort?', 'O(n log n)', { topic: 'complexity' }), { due: at({ hours: -5 }) }],
    [
      'traversal',
      q('mcq', 'Which traversal visits a graph level by level?', 'Breadth-first search', {
        choices: ['Depth-first search', 'Breadth-first search', 'Topological sort'],
        topic: 'graphs',
      }),
      { due: at({ hours: -4 }) },
    ],
    [
      'binary_search',
      q('code', 'Return the index of `target` in the sorted list `xs`, or -1.', BINARY_SEARCH_ANSWER, {
        language: 'python',
        skeleton: 'def find(xs, target):\n    ...\n',
        rubric: '- Halves the range each step\n- Returns -1 on a miss',
        topic: 'searching',
      }),
      { due: at({ hours: -3 }), step: 2, last_review: at({ days: -3 }) },
    ],
    [
      'annotated',
      q('short', 'Which sort is stable: heapsort or merge sort?', 'Merge sort', {
        answer_regex: '(?i)merge',
        explanation: 'Merge sort keeps equal keys in input order; heapsort does not.',
        topic: 'sorting',
      }),
      { due: at({ days: 2 }), step: 3, last_review: at({ days: -6 }) },
    ],
    ['retired', q('short', 'Which sort did the 1959 Shell paper describe?', 'Shellsort', { topic: 'history' }), { due: at({ days: 4 }), step: 1, last_review: at({ days: -8 }) }],
    ['duplicate', q('short', 'What is the average-case cost of quicksort?', 'O(n log n)', { topic: 'complexity' }), { due: at({ days: 7 }) }],
  ];
  const aIds = insertCards(ctx, a, aCards);

  const b = repos.decks.create('databases', { contextPrompt: 'Storage engines, indexes and transactions.', displayName: 'Databases' });
  const bIds = insertCards(ctx, b, [
    ['acid', q('short', 'What does the I in ACID guarantee?', "Concurrent transactions do not observe each other's partial writes.", { topic: 'transactions' }), { due: at({ days: 1 }) }],
    [
      'btree',
      q('mcq', 'Which index shape keeps range scans sequential on disk?', 'B-tree', { choices: ['Hash index', 'B-tree', 'Bloom filter'], topic: 'indexes' }),
      { due: at({ days: 3 }), step: 1, last_review: at({ days: -2 }) },
    ],
    [
      'wal',
      q('short', 'Why does a write-ahead log make crash recovery possible?', 'The log records an intent before the page changes, so recovery replays it.', { topic: 'durability' }),
      { due: at({ days: 6 }), step: 2, last_review: at({ days: -7 }) },
    ],
  ]);

  const t = repos.decks.createTrivia('systems-trivia', { topic: 'Operating systems and computer architecture.', intervalMinutes: 1440, displayName: 'Systems Trivia' });

  const sid = await repos.sessions.create(a, DEVICE_LABEL);
  repos.pins.session(sid, at({ minutes: -2 }), at({ minutes: -6 }));

  return {
    decks: {
      srs_a: { id: a, slug: 'algorithms', display: 'Algorithms' },
      srs_b: { id: b, slug: 'databases', display: 'Databases' },
      trivia: { id: t, slug: 'systems-trivia', display: 'Systems Trivia' },
    },
    questions: { srs_a: aIds, srs_b: bIds },
    session_id: sid,
  };
}
