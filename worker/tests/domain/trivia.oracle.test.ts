import { describe, expect, it } from 'vitest';
import { flipDoneVerdict, formatDone, parseCardIds, parseDone, type DoneItem } from '../../domain/trivia.js';
import { pythonJson } from '../pyoracle.js';

const CARDS = ['1,2,3', '', ' 4 , 5,,x,6a,-1,1.5, 007', '49', '1,　2　,3', '\t7\n,8\r', '12345678901234'];
const DONE = ['42r,17w,99r', '', 'r,1,1x,1R, 2w ,r3,1.5r,-1w', '　5r　,6w', '0r,00w', '42rr,17ww'];
const ITEMS: DoneItem[][] = [[], [[42, 'r']], [[42, 'r'], [17, 'w'], [99, 'r']], [[1, 'w'], [1, 'r']]];
const FLIPS: [DoneItem[], number, boolean][] = [
  [ITEMS[2]!, 17, true],
  [ITEMS[2]!, 42, false],
  [ITEMS[2]!, 7, true],
  [ITEMS[3]!, 1, true],
  [[], 1, false],
];

interface Oracle {
  cards: number[][];
  done: [number, string][][];
  format: string[];
  flip: string[];
}

const payload = Buffer.from(JSON.stringify({ cards: CARDS, done: DONE, items: ITEMS, flips: FLIPS })).toString('base64');
const oracle = pythonJson<Oracle>(`
import base64, json
from prep.trivia.session_state import parse_card_ids, parse_done, format_done, flip_done_verdict
c = json.loads(base64.b64decode("${payload}"))
print(json.dumps({
  "cards": [parse_card_ids(s) for s in c["cards"]],
  "done": [parse_done(s) for s in c["done"]],
  "format": [format_done([tuple(i) for i in items]) for items in c["items"]],
  "flip": [flip_done_verdict([tuple(i) for i in items], qid, ok) for items, qid, ok in c["flips"]],
}))
`);

describe('trivia session state matches the reference', () => {
  it('parse_card_ids', () => {
    expect(CARDS.map(parseCardIds)).toEqual(oracle.cards);
  });
  it('parse_done', () => {
    expect(DONE.map(parseDone)).toEqual(oracle.done);
  });
  it('format_done', () => {
    expect(ITEMS.map(formatDone)).toEqual(oracle.format);
  });
  it('flip_done_verdict', () => {
    expect(FLIPS.map((args) => flipDoneVerdict(...args))).toEqual(oracle.flip);
  });
});
