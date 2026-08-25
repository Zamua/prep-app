import type { FixturePage, FixturePages } from '../../app/ports.js';
import corpus from '../../build/pages.js';

export interface RecordedPage extends FixturePage {
  profile: string;
  name: string;
}

/** The shape `scripts/build-pages.mjs` emits from tests/fixtures/parity/pages. */
export interface Corpus {
  seeds: Record<string, Record<string, unknown>>;
  pages: RecordedPage[];
}

function pathname(path: string): string {
  const q = path.indexOf('?');
  return q < 0 ? path : path.slice(0, q);
}

function stateFlags(state: string | null): string[] {
  return state ? state.split('+') : [];
}

/** The page recorded for the request whose state the flags satisfy; a page
 * recorded under a flag beats the flagless recording. Ties keep recording
 * order. */
export function resolvePage(
  pages: readonly RecordedPage[],
  profile: string,
  method: string,
  path: string,
  flags: readonly string[],
): FixturePage | null {
  const want = pathname(path);
  let best: RecordedPage | null = null;
  let bestRank = -1;
  for (const page of pages) {
    if (page.profile !== profile || page.method !== method || pathname(page.path) !== want) continue;
    const needs = stateFlags(page.state);
    if (!needs.every((f) => flags.includes(f))) continue;
    if (needs.length > bestRank) {
      best = page;
      bestRank = needs.length;
    }
  }
  return best;
}

export class CorpusFixturePages implements FixturePages {
  constructor(private readonly corpus: Corpus) {}

  seed(profile: string): Record<string, unknown> | null {
    return this.corpus.seeds[profile] ?? null;
  }

  resolve(profile: string, method: string, path: string, flags: readonly string[]): FixturePage | null {
    return resolvePage(this.corpus.pages, profile, method, path, flags);
  }
}

export function fixturePagesFromBuild(): FixturePages {
  return new CorpusFixturePages(corpus);
}
