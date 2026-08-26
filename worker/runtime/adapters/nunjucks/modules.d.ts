// Ambient modules for the nunjucks adapter: the slim runtime ships no
// types, and the baked maps under build/ exist only after `npm run build`.

declare module "nunjucks/browser/nunjucks-slim.js" {
  export class SafeString {
    constructor(val: string);
    val: string;
    length: number;
    toString(): string;
    valueOf(): string;
  }
  export interface Runtime {
    SafeString: typeof SafeString;
    markSafe(val: unknown): unknown;
    copySafeness(dest: unknown, target: unknown): unknown;
  }
  export class PrecompiledLoader {
    constructor(compiled: Record<string, unknown>);
  }
  export class Environment {
    constructor(loader: PrecompiledLoader | null, opts?: { autoescape?: boolean; throwOnUndefined?: boolean });
    addFilter(name: string, fn: (...args: any[]) => unknown, async?: boolean): this;
    addGlobal(name: string, value: unknown): this;
    addTest(name: string, fn: (...args: any[]) => boolean): this;
    render(name: string, context: Record<string, unknown>): string;
  }
  const nunjucks: {
    Environment: typeof Environment;
    PrecompiledLoader: typeof PrecompiledLoader;
    runtime: Runtime;
  };
  export default nunjucks;
}
