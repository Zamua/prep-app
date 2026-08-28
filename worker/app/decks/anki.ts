// Anki `.apkg` import. One note becomes one short-type card: first field is
// the prompt, the rest join into the answer, HTML and media references are
// dropped, cloze notes are skipped. Reading the zipped sqlite is the
// `ApkgReader` port's job; this half is pure.
import type { NewQuestion } from '../entities.js';
import type { ApkgNote, DeckRepo, QuestionRepo } from '../ports.js';
import { rowCapMessage } from './importLimits.js';

/** Anki's field separator in `notes.flds` (record separator, 0x1f). */
const FIELD_SEP = '\x1f';

const CLOZE_RE = /\{\{c\d+::/;

const BR_RE = /<\s*br\s*\/?\s*>/gi;
const BLOCK_END_RE = /<\/\s*(?:p|div|li|h[1-6])\s*>/gi;
const TAG_RE = /<[^>]+>/g;
const MEDIA_RE = /\[(?:sound|anki):[^\]]*\]/gi;

/** Order matters: `&amp;` runs after `&nbsp;`, so a literal `&amp;nbsp;`
 * survives as `&nbsp;` rather than collapsing to a space. */
const ENTITIES: readonly (readonly [string, string])[] = [
  ['&nbsp;', ' '],
  ['&amp;', '&'],
  ['&lt;', '<'],
  ['&gt;', '>'],
  ['&quot;', '"'],
  ['&#39;', "'"],
  ['&apos;', "'"],
];

/** Split on every Unicode line boundary, not just `\n`, and no empty tail. */
function splitLines(s: string): string[] {
  const lines = s.split(/\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/);
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

export function stripHtml(s: string): string {
  if (!s) return '';
  let out = s.replace(BR_RE, '\n').replace(BLOCK_END_RE, '\n').replace(MEDIA_RE, '').replace(TAG_RE, '');
  for (const [from, to] of ENTITIES) out = out.split(from).join(to);
  const kept: string[] = [];
  let prevBlank = false;
  for (const raw of splitLines(out)) {
    const ln = raw.trim();
    if (!ln) {
      if (prevBlank) continue;
      prevBlank = true;
    } else {
      prevBlank = false;
    }
    kept.push(ln);
  }
  return kept.join('\n').trim();
}

/** Parallels `ImportOutcome`; `cloze_skipped` is broken out because a cloze
 * note is expected, not malformed. */
export interface AnkiImportOutcome {
  deck_id: number;
  deck_name: string;
  inserted: number;
  skipped_duplicates: number;
  cloze_skipped: number;
  errors: string[];
}

export interface AnkiImportRepos {
  decks: DeckRepo;
  questions: QuestionRepo;
}

export function ankiNotesToDeck(
  repos: AnkiImportRepos,
  deckName: string,
  notes: readonly ApkgNote[],
  opts: { noteCap?: number } = {},
): AnkiImportOutcome {
  const deckId = repos.decks.getOrCreate(deckName);
  const existing = new Set(repos.questions.promptsInDeck(deckId));

  let inserted = 0;
  let skippedDuplicates = 0;
  let clozeSkipped = 0;
  let seen = 0;
  const errors: string[] = [];
  const cap = opts.noteCap ?? Infinity;

  for (const note of notes) {
    if (seen >= cap) {
      errors.push(rowCapMessage(cap));
      break;
    }
    seen++;

    const fields = (note.flds || '').split(FIELD_SEP);
    if (!fields.length || !fields[0]) {
      errors.push(`note ${note.id}: empty fields`);
      continue;
    }

    const rawFront = fields[0]!;
    if (CLOZE_RE.test(rawFront)) {
      clozeSkipped++;
      continue;
    }

    const prompt = stripHtml(rawFront);
    if (!prompt) {
      errors.push(`note ${note.id}: empty prompt after HTML strip`);
      continue;
    }
    if (existing.has(prompt)) {
      skippedDuplicates++;
      continue;
    }

    // A single-field note leaves nothing for the back; the user is told which
    // note was dropped rather than getting a card with no answer.
    const answer = stripHtml(fields.slice(1).filter(Boolean).join('\n\n'));
    if (!answer) {
      errors.push(`note ${note.id}: no back-side content`);
      continue;
    }

    const question: NewQuestion = { type: 'short', prompt, answer };
    try {
      repos.questions.add(deckId, question);
      existing.add(prompt);
      inserted++;
    } catch (e) {
      errors.push(`note ${note.id}: write failed — ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return {
    deck_id: deckId,
    deck_name: deckName,
    inserted,
    skipped_duplicates: skippedDuplicates,
    cloze_skipped: clozeSkipped,
    errors,
  };
}
