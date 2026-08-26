// Named-deck creation over the sync path: the slug is the kebab form of the
// label; a label with no sluggable characters takes the generic base.

export const INBOX_DECK_NAME = 'inbox';
export const DECK_SLUG_FALLBACK = 'deck';
export const MAX_DECK_SLUG_ATTEMPTS = 100;

export function slugForDeckName(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug || DECK_SLUG_FALLBACK;
}
