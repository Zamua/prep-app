// Its own file: the engine is memoised per module instance, and this test needs
// a fresh one loaded under a Workers-shaped global.
import { afterEach, describe, expect, it, vi } from 'vitest';

const g = globalThis as Record<string, unknown>;

describe('sqlEngine in a Workers isolate', () => {
  afterEach(() => {
    delete g['WorkerGlobalScope'];
  });

  it('starts where WorkerGlobalScope exists and location does not, and leaves no location behind', async () => {
    g['WorkerGlobalScope'] = class {};
    g['self'] ??= globalThis;
    expect('location' in globalThis).toBe(false);
    vi.resetModules();
    const { sqlEngine } = await import('../runtime/adapters/apkg.js');
    const SQL = (await sqlEngine()) as unknown as { Database: new () => { exec(sql: string): { values: unknown[][] }[] } };
    expect(new SQL.Database().exec('select 1')[0]!.values).toEqual([[1]]);
    expect('location' in globalThis).toBe(false);
  });
});
