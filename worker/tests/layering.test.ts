import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';

// The dependency rule: runtime -> app -> domain, and nothing imports upward.
// Enforced by a test because the rule is only worth having if breaking it is
// noisy.
const ROOT = new URL('..', import.meta.url).pathname;

function tsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...tsFiles(p));
    else if (name.endsWith('.ts') && !name.endsWith('.d.ts')) out.push(p);
  }
  return out;
}

function importsOf(file: string): string[] {
  const src = readFileSync(file, 'utf8');
  return [...src.matchAll(/(?:from|import)\s+['"]([^'"]+)['"]/g)].map((m) => m[1]!);
}

/** Layer-relative path of a relative import, or null for a bare specifier. */
function target(file: string, spec: string): string | null {
  if (!spec.startsWith('.')) return null;
  return relative(ROOT, resolve(dirname(file), spec));
}

const rel = (f: string) => f.slice(ROOT.length);

describe('the dependency rule holds', () => {
  let checked = 0;

  it('domain imports nothing from app, runtime, cloudflare: or node:', () => {
    const files = tsFiles(join(ROOT, 'domain'));
    expect(files.length).toBeGreaterThan(0);
    for (const f of files) {
      checked++;
      for (const im of importsOf(f)) {
        const t = target(f, im);
        const bad = /^cloudflare:/.test(im) || /^node:/.test(im) || (t !== null && !t.startsWith('domain/'));
        expect(bad, `${rel(f)} imports ${im}, which domain may not`).toBe(false);
      }
    }
  });

  it('app imports only ../domain/ and ./', () => {
    const files = tsFiles(join(ROOT, 'app'));
    expect(files.length).toBeGreaterThan(0);
    for (const f of files) {
      checked++;
      for (const im of importsOf(f)) {
        const t = target(f, im);
        const ok = t !== null && (t.startsWith('domain/') || t.startsWith('app/'));
        expect(ok, `${rel(f)} imports ${im}, which app may not`).toBe(true);
      }
      const src = readFileSync(f, 'utf8');
      for (const smell of ['fetch(', 'new Response', '.sql.exec', 'DurableObject', 'nunjucks']) {
        expect(src.includes(smell), `${rel(f)} contains ${smell}`).toBe(false);
      }
    }
  });

  // Cells and the router receive adapters through the ports from the
  // composition root; naming an adapter anywhere else is the drift the ports
  // exist to prevent.
  it('only the composition root imports adapters', () => {
    for (const f of tsFiles(join(ROOT, 'runtime'))) {
      checked++;
      const me = rel(f);
      if (me === 'runtime/compose.ts' || me.startsWith('runtime/adapters/')) continue;
      for (const im of importsOf(f)) {
        const t = target(f, im);
        expect(t?.startsWith('runtime/adapters/') ?? false, `${me} imports adapter ${im}`).toBe(false);
      }
    }
  });

  it('only the nunjucks adapter imports nunjucks or the compiled templates', () => {
    for (const layer of ['domain', 'app', 'runtime']) {
      for (const f of tsFiles(join(ROOT, layer))) {
        if (rel(f).startsWith('runtime/adapters/nunjucks/')) continue;
        for (const im of importsOf(f)) {
          const bad = im === 'nunjucks' || im.startsWith('nunjucks/') || /build\/templates\.js$/.test(im);
          expect(bad, `${rel(f)} imports ${im}`).toBe(false);
        }
      }
    }
  });

  it('actually inspected every layer', () => {
    expect(checked).toBeGreaterThanOrEqual(3);
    for (const layer of ['domain', 'app', 'runtime']) expect(tsFiles(join(ROOT, layer)).length).toBeGreaterThan(0);
  });
});
