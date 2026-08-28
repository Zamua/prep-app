// The "discuss this card" payload: one prefilled chat URL per provider. The
// message embeds the whole card context, so it is composed server-side.

export const CHAT_PROVIDERS: Record<string, { label: string; url: string }> = {
  claude: { label: 'Claude', url: 'https://claude.ai/new?q={q}' },
  chatgpt: { label: 'ChatGPT', url: 'https://chatgpt.com/?q={q}' },
  perplexity: { label: 'Perplexity', url: 'https://www.perplexity.ai/?q={q}' },
};

export const DEFAULT_PROVIDER = 'claude';

// Long code answers blow past mobile-browser URL caps; each section is
// truncated so the total stays well under them.
const MAX_FIELD_CHARS = 4000;

const TRAILING_SPACE = /\s+$/;

function trim(s: unknown): string {
  if (typeof s !== 'string' || !s) return '';
  const chars = [...s];
  if (chars.length <= MAX_FIELD_CHARS) return s;
  return chars.slice(0, MAX_FIELD_CHARS).join('').replace(TRAILING_SPACE, '') + '\n\u2026[truncated]';
}

export interface HandoffQuestion {
  type?: string;
  prompt?: string;
  answer?: string;
  rubric?: string | null;
  choices_list?: string[];
}

export function buildMessage(opts: {
  deckName: string;
  q: HandoffQuestion;
  userAnswer?: string;
  verdict?: Record<string, unknown> | null;
  idk?: boolean;
  pickedSet?: string[];
  correctSet?: string[];
}): string {
  const { deckName, q } = opts;
  const userAnswer = opts.userAnswer ?? '';
  const verdict = opts.verdict ?? null;
  const idk = Boolean(opts.idk);
  const picked = opts.pickedSet ?? [];
  const correct = opts.correctSet ?? [];
  const qtype = q.type ?? 'short';
  const parts: string[] = [];
  parts.push("I'm reviewing a flashcard and want to talk through it.\n");
  parts.push(`**Question** (deck: \`${deckName}\`, type: \`${qtype}\`):`);
  parts.push(trim(q.prompt ?? ''));

  const choices = q.choices_list ?? [];
  if ((qtype === 'mcq' || qtype === 'multi') && choices.length) {
    parts.push('\n**Choices:**');
    for (const c of choices) {
      let mark = '';
      if (picked.length && picked.includes(c)) mark = ' \u2190 my pick';
      if (correct.length && correct.includes(c)) mark += ' \u2713 correct';
      parts.push(`- ${c}${mark}`);
    }
  }

  if (idk) {
    parts.push("\n**My answer:** _(I don't know \u2014 skipped)_");
  } else if (userAnswer && qtype !== 'mcq' && qtype !== 'multi') {
    parts.push('\n**My answer:**');
    if (qtype === 'code') {
      parts.push('```');
      parts.push(trim(userAnswer));
      parts.push('```');
    } else {
      parts.push(trim(userAnswer));
    }
  }

  if (q.answer && qtype !== 'mcq' && qtype !== 'multi') {
    parts.push('\n**Model answer:**');
    if (qtype === 'code') {
      parts.push('```');
      parts.push(trim(q.answer));
      parts.push('```');
    } else {
      parts.push(trim(q.answer));
    }
  }

  if (verdict && Object.keys(verdict).length) {
    parts.push(`\n**Verdict:** ${verdict['result'] ?? 'unknown'}`);
    if (verdict['feedback']) parts.push(`**Feedback:** ${trim(verdict['feedback'])}`);
  }

  if (q.rubric) parts.push(`\n**Rubric:** ${trim(q.rubric)}`);

  parts.push('\nPlease explain.');
  return parts.join('\n');
}

/** `urllib.parse.quote(s, safe="")`: only unreserved characters survive. */
export function quoteAll(s: string): string {
  return encodeURIComponent(s).replace(/[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase());
}

export function providerUrls(message: string): Record<string, string> {
  const encoded = quoteAll(message);
  const out: Record<string, string> = {};
  for (const [key, cfg] of Object.entries(CHAT_PROVIDERS)) out[key] = cfg.url.replace('{q}', encoded);
  return out;
}

export function providerLabels(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, cfg] of Object.entries(CHAT_PROVIDERS)) out[key] = cfg.label;
  return out;
}
