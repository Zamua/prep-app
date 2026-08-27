import { describe, expect, it } from 'vitest';
import { DurabilityUnproven, isRefusal } from '../domain/jobs/refusal';

describe('a refusal that reached us wrapped', () => {
  it('is still a refusal when the name survives only in the message', () => {
    expect(isRefusal(new Error('route failed: DurabilityUnproven'))).toBe(true);
    expect(isRefusal(new Error('route failed: DurableObjectRoutingError'))).toBe(true);
  });

  it('is still a refusal when it hides in the cause chain', () => {
    const inner = new DurabilityUnproven('durability unproven: no quorum');
    expect(isRefusal(new Error('route failed', { cause: inner }))).toBe(true);
    expect(isRefusal(new Error('outer', { cause: new Error('mid', { cause: inner }) }))).toBe(true);
  });

  it('does not turn an ordinary failure into a retry', () => {
    expect(isRefusal(new Error('deck not found'))).toBe(false);
    expect(isRefusal(new Error('wrapped', { cause: new Error('deck not found') }))).toBe(false);
  });

  it('terminates on a cause cycle', () => {
    const a = new Error('a');
    (a as { cause?: unknown }).cause = a;
    expect(isRefusal(a)).toBe(false);
  });
});
