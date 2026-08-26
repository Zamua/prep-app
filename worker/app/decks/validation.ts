// Form-level validation for the deck surfaces. Stricter than the entity:
// user-typed names pass here first, so arbitrary unicode, spaces and
// uppercase never reach a slug, while legacy rows keep their looser shapes.
import { SLUG_ALPHABET, SLUG_LENGTH } from '../entities.js';
import { AppError, badRequest } from '../errors.js';
import type { DeckRepo, Random } from '../ports.js';

const DECK_NAME_RE = /^[a-z0-9][a-z0-9-]{1,29}$/;

const RESERVED_DECK_NAMES = new Set([
  'new',
  'create',
  'edit',
  'delete',
  'static',
  'dev',
  'preview',
  'notify',
  'session',
  'study',
  'deck',
  'decks',
  'manifest',
]);

export const MAX_CONTEXT_PROMPT_CHARS = 8000;
export const MAX_DISPLAY_NAME_CHARS = 60;
export const MAX_TOPIC_PROMPT_CHARS = 4000;

export function validateDeckName(name: string): string {
  const n = (name || '').trim().toLowerCase();
  if (!DECK_NAME_RE.test(n)) {
    throw badRequest(
      'Deck name must be 2-30 chars, lowercase, alphanumerics or hyphens, starting with a letter or digit.',
    );
  }
  if (RESERVED_DECK_NAMES.has(n)) throw badRequest(`"${n}" is reserved — pick another name.`);
  return n;
}

/** The user-typed label: spaces, capitals and punctuation allowed. */
export function validateDisplayName(name: string): string {
  const n = (name || '').trim();
  if (!n) throw badRequest('Deck name is required.');
  if (n.includes('\n') || n.includes('\r')) throw badRequest("Deck name can't contain newlines.");
  if (n.length > MAX_DISPLAY_NAME_CHARS) {
    throw badRequest(`Deck name is too long (${n.length} chars; max ${MAX_DISPLAY_NAME_CHARS}).`);
  }
  return n;
}

/** An opaque slug so renames never break links. 100 collisions in a row
 * means the repo is lying, not that the space is full. */
export function uniqueSlug(decks: DeckRepo, random: Random): string {
  for (let i = 0; i < 100; i++) {
    let candidate = '';
    for (let j = 0; j < SLUG_LENGTH; j++) candidate += random.choice([...SLUG_ALPHABET]);
    if (decks.findId(candidate) === null) return candidate;
  }
  throw new AppError(500, 'could not find a free deck slug after 100 attempts');
}
