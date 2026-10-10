import { describe, expect, it } from 'vitest';
import { CELL_IMAGE_FORMAT, CELL_IMAGE_VERSION, InvalidCellImage, parseCellImage } from '../../domain/cellImage.js';

const image = (over: Record<string, unknown> = {}) => ({
  format: CELL_IMAGE_FORMAT,
  version: CELL_IMAGE_VERSION,
  tables: { decks: { columns: ['rowid', 'id', 'name', 'icon'], rows: [[1, 1, 'geo', { b64: 'AQI=' }]] } },
  sequence: { decks: 3 },
  kv: { reap: { cursor: null } },
  ...over,
});

describe('parseCellImage', () => {
  it('accepts a well-formed image unchanged', () => {
    expect(parseCellImage(image())).toEqual(image());
  });

  it.each([
    ['another format', { format: 'kcal.diary' }],
    ['another version', { version: 2 }],
    ['a table name SQL would have to quote', { tables: { 'decks; drop': { columns: ['rowid'], rows: [] } } }],
    ['a column name SQL would have to quote', { tables: { decks: { columns: ['rowid', 'a"b'], rows: [] } } }],
    ['a table without its rowid first', { tables: { decks: { columns: ['id'], rows: [] } } }],
    ['a row of the wrong width', { tables: { decks: { columns: ['rowid', 'id'], rows: [[1]] } } }],
    ['a fractional rowid', { tables: { decks: { columns: ['rowid'], rows: [[1.5]] } } }],
    ['a value SQLite cannot hold', { tables: { decks: { columns: ['rowid', 'id'], rows: [[1, { nested: true }]] } } }],
    ['a counter for a table it does not carry', { sequence: { cards: 1 } }],
    ['a negative counter', { sequence: { decks: -1 } }],
  ])('refuses %s', (_name, over) => {
    expect(() => parseCellImage(image(over))).toThrow(InvalidCellImage);
  });
});
