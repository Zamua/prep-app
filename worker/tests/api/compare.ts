// Comparing a replayed response with its recorded one. Values another
// implementation cannot reproduce (a minted cookie, a random slug, the
// VAPID key) are named by the corpus header and compared by regex; a
// rendered page is compared with its whitespace collapsed, since a fragment
// carries indentation that no reader depends on.

export interface VolatileRule {
  pairs: string;
  pointer: string;
  regex: string;
}

const MASK = '<VOLATILE>';

function globMatches(name: string, glob: string): boolean {
  const src = glob
    .split('')
    .map((ch) => (ch === '*' ? '.*' : ch === '?' ? '.' : ch.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    .join('');
  return new RegExp(`^${src}$`).test(name);
}

/** Replace every regex match under `pointer` with the mask, in place. */
function maskAt(node: unknown, parts: string[], re: RegExp): void {
  if (node === null || node === undefined) return;
  const [head, ...rest] = parts;
  if (head === undefined) return;
  const container = node as Record<string, unknown>;
  const keys = head === '*' ? Object.keys(container) : [head];
  for (const key of keys) {
    const value = container[key];
    if (value === undefined) continue;
    if (rest.length) {
      maskAt(value, rest, re);
      continue;
    }
    if (typeof value === 'string') container[key] = value.replace(re, MASK);
    // An id is an integer on the wire; a rule that matches the whole of it
    // masks it the same way, which is how a block id is compared.
    else if (typeof value === 'number' && new RegExp(re.source).test(String(value))) container[key] = MASK;
  }
}

export function applyVolatile(name: string, response: Record<string, unknown>, rules: readonly VolatileRule[]): void {
  for (const rule of rules) {
    if (!globMatches(name, rule.pairs)) continue;
    const parts = rule.pointer.split('.');
    if (parts[0] !== 'response') continue;
    maskAt({ response }, parts, new RegExp(rule.regex, 'g'));
  }
}

/** The comparable shape of one response, volatile values masked. */
export function comparable(
  name: string,
  value: { status: number; json: unknown; text: string | null; location: string | null; setCookie: string[] },
  rules: readonly VolatileRule[],
): Record<string, unknown> {
  const response: Record<string, unknown> = {
    status: value.status,
    json: value.json ?? null,
    text: value.text,
    location: value.location,
    set_cookie: [...value.setCookie],
  };
  applyVolatile(name, response, rules);
  if (typeof response['text'] === 'string') response['text'] = collapse(response['text'] as string);
  // A minted cookie carries a fresh value and a fresh expiry; only its
  // presence and its name are reproducible.
  response['set_cookie'] = (response['set_cookie'] as string[]).map((c) => collapse(c));
  return response;
}

/** Whitespace between tags is not content; nunjucks and jinja space it differently. */
export function collapse(html: string): string {
  return html.replace(/\s+/g, ' ').replace(/> </g, '><').trim();
}
