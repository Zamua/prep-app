import { describe, expect, it } from 'vitest';
import { buildPrompt, displayNameFor, parseQaPairs, sanitizeTopic } from '../../domain/instant/cards.js';
import { pythonJson } from '../pyoracle.js';

const SANITIZE: unknown[] = [
  'Roman history',
  '  padded  ',
  'tabs\tand\nnewlines\r\nhere',
  'nul\x00bell\x07del\x7fnel\x85end',
  '\u3000ideographic space\u3000',
  '\xa0nbsp\xa0',
  'zero width\u200bjoiner\u200d',
  '',
  '   ',
  '\x00\x01',
  null,
  5,
  ['a'],
  { a: 1 },
  true,
  'a'.repeat(500),
  'a'.repeat(501),
  'a'.repeat(1000),
  'a'.repeat(1001),
  'a'.repeat(400) + '\x00'.repeat(600),
  'a'.repeat(400) + '\x00'.repeat(601),
  '😀'.repeat(500),
  '😀'.repeat(501),
  '  ' + 'b'.repeat(500) + '  ',
  'é'.repeat(300),
];

const DISPLAY = [
  'Roman history',
  '  many   spaces\t\tand\nlines  ',
  'x'.repeat(59) + ' ' + 'y'.repeat(10),
  'x'.repeat(60) + 'y',
  '😀'.repeat(70),
  '　wide　　gap　',
  'a\x85b\x1cc',
  'short',
];

const PROMPT = ['Roman history', 'topic with % and %(topic)s and `backticks`'];

const PARSE = [
  '[{"q": "Q?", "a": "A", "r": null}]',
  '```json\n[{"q": "Q?", "a": "A"}]\n```',
  '```JSON\n[1, 2]\n```',
  '```\n[1]\n```',
  '  \n```json  [1]  ```  \n',
  'Here is the JSON you asked for:\n[{"q": "Q?"}]\nHope this helps!',
  'no array here',
  '] before [',
  '',
  '[1, 2',
  '{"a": [1]}',
  '{"a": 1}',
  '[[1], [2]]',
  '[] trailing ] bracket',
  '```json\n```',
  '[1] ``` middle ``` [2]',
  '[1]```',
  '["é", "😀"]',
  '[1,]',
  '[1] ```json',
];

interface Oracle {
  sanitize: (string | null)[];
  display: string[];
  prompt: string[];
  parse: ({ ok: unknown[] } | { error: true })[];
}

const payload = Buffer.from(JSON.stringify({ sanitize: SANITIZE, display: DISPLAY, prompt: PROMPT, parse: PARSE })).toString('base64');
const oracle = pythonJson<Oracle>(`
import base64, json
from prep.instant.service import sanitize_topic, display_name_for, build_prompt
from prep.domain.qa_extract import parse_qa_pairs
c = json.loads(base64.b64decode("${payload}"))
parse = []
for s in c["parse"]:
    try:
        parse.append({"ok": parse_qa_pairs(s)})
    except ValueError:
        parse.append({"error": True})
print(json.dumps({
  "sanitize": [sanitize_topic(x) for x in c["sanitize"]],
  "display": [display_name_for(x) for x in c["display"]],
  "prompt": [build_prompt(x) for x in c["prompt"]],
  "parse": parse,
}))
`);

describe('instant hygiene matches the reference', () => {
  it('sanitize_topic', () => {
    expect(SANITIZE.map(sanitizeTopic)).toEqual(oracle.sanitize);
    expect(oracle.sanitize.filter((x) => x === null).length).toBeGreaterThanOrEqual(12);
  });

  it('display_name_for', () => {
    expect(DISPLAY.map(displayNameFor)).toEqual(oracle.display);
  });

  it('build_prompt', () => {
    expect(PROMPT.map(buildPrompt)).toEqual(oracle.prompt);
  });

  it('parse_qa_pairs, every rejection branch included', () => {
    const got = PARSE.map((s) => {
      try {
        return { ok: parseQaPairs(s) };
      } catch {
        return { error: true as const };
      }
    });
    expect(got).toEqual(oracle.parse);
    expect(oracle.parse.filter((x) => 'error' in x).length).toBeGreaterThanOrEqual(6);
  });
});
