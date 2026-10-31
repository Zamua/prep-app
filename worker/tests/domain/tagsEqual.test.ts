import { describe, expect, it } from 'vitest';
import { tagsEqual } from '../../domain/tagsEqual.js';

describe('tagsEqual', () => {
  it('is string equality, lengths included', () => {
    expect(tagsEqual('abc', 'abc')).toBe(true);
    expect(tagsEqual('', '')).toBe(true);
    expect(tagsEqual('abc', 'abd')).toBe(false);
    expect(tagsEqual('abc', 'ab')).toBe(false);
    expect(tagsEqual('ab', 'abc')).toBe(false);
    expect(tagsEqual('', 'a')).toBe(false);
    expect(tagsEqual('a\u0000', 'a')).toBe(false);
  });
});
