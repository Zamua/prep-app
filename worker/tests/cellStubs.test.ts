import { describe, expect, it } from 'vitest';
import { namespaceDirectory, NoRpcMethod } from '../runtime/adapters/cells.js';

/** A Workers RPC stub: every member is itself an RPC proxy, so reading
 * `.apply` off a method names a remote method called "apply". */
function workersStub(methods: Record<string, (...a: unknown[]) => unknown>, calls: string[]): object {
  const member = (name: string): unknown =>
    new Proxy(function () {}, {
      apply: async (_t, _this, args: unknown[]) => {
        calls.push(name);
        const m = methods[name];
        if (!m) throw new TypeError(`The RPC receiver does not implement the method "${name}".`);
        return m(...args);
      },
      get: (_t, prop) => member(String(prop)),
    });
  return new Proxy({}, { get: (_t, prop) => member(String(prop)) });
}

const namespace = (stub: object) => ({ idFromName: (n: string) => n, get: () => stub }) as unknown as DurableObjectNamespace;
const IMMEDIATE = { attempts: 5, baseMs: 0, sleep: async () => {} };

describe('a cell port over a Workers RPC stub', () => {
  it('calls the method itself, with its arguments', async () => {
    const calls: string[] = [];
    const port = namespaceDirectory(namespace(workersStub({ echo: (a, b) => [a, b] }, calls)), IMMEDIATE) as unknown as {
      echo(a: number, b: string): Promise<unknown>;
    };
    await expect(port.echo(1, 'x')).resolves.toEqual([1, 'x']);
    expect(calls).toEqual(['echo']);
  });

  it('reports a method the class lacks as NoRpcMethod, without retrying', async () => {
    const calls: string[] = [];
    const port = namespaceDirectory(namespace(workersStub({}, calls)), IMMEDIATE) as unknown as { missing(): Promise<unknown> };
    await expect(port.missing()).rejects.toBeInstanceOf(NoRpcMethod);
    expect(calls).toEqual(['missing']);
  });
});
