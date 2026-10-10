// Workflow id shapes. The routes parse them for ownership, so a change here
// is a change to what a deep link means, and links already in the wild stop
// resolving.

export const SUFFIX_HEX_CHARS = 10;

export const gradeId = (deckName: string, questionId: number, hex: string): string => `grade-${deckName}-q${questionId}-${hex}`;
export const transformId = (scope: string, targetId: number, hex: string): string => `transform-${scope}-${targetId}-${hex}`;
export const planId = (deckName: string, hex: string): string => `plan-${deckName}-${hex}`;
export const triviaId = (deckName: string, hex: string): string => `trivia-${deckName}-${hex}`;

/** A caller key as an id suffix. The routes split an id from the right, so
 * the suffix holds no hyphen; the escape is injective, so two keys never
 * share an id. */
export const encodeIdempotencyKey = (key: string): string => key.replaceAll('z', 'zz').replaceAll('-', 'zx');

/** The id a keyed grade start gets. A session can outlive a deck rename, so
 * the id cannot carry the mutable deck name. */
export const keyedGradeId = (questionId: number, key: string): string => gradeId('session', questionId, encodeIdempotencyKey(key));
