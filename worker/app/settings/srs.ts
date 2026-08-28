// /settings/srs: the FSRS retention target. A small preset set rather
// than a free slider, because the algorithm misbehaves at the extremes.
import { DEFAULT_DESIRED_RETENTION, MAX_DESIRED_RETENTION, MIN_DESIRED_RETENTION } from '../../domain/fsrs/index.js';
import { literal } from '../../domain/grading/literal.js';
import { badRequest } from '../errors.js';
import { page, type PageRequest, type PageResult } from '../pageResult.js';
import type { UserRepos } from '../ports.js';

export type RetentionPreset = readonly [value: number, label: string, blurb: string];

export const RETENTION_PRESETS: readonly RetentionPreset[] = [
  [0.8, '80% — Relaxed', 'Fewer reviews; more cards slip through.'],
  [0.85, '85% — Mild', 'Slightly less frequent; light maintenance.'],
  [0.9, "90% — Default", "Anki's default. Balances retention vs work."],
  [0.95, '95% — Strict', 'Tighter recall, more frequent reviews.'],
];

/** A rate as a whole percent, for the bounds message. */
const pct = (x: number): string => `${Math.round(x * 100)}%`;

export const RETENTION_RANGE_MESSAGE = `retention must be between ${pct(MIN_DESIRED_RETENTION)} and ${pct(MAX_DESIRED_RETENTION)}`;

const FLOAT = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
const NON_FINITE = /^[+-]?(inf(inity)?|nan)$/i;

/** A float literal, including the non-finite spellings; null when the
 * text is not one. */
export function parseFloatLiteral(raw: string): number | null {
  const s = raw.trim();
  if (FLOAT.test(s)) return Number(s);
  if (NON_FINITE.test(s)) return s.replace(/^[+-]/, '').toLowerCase() === 'nan' ? NaN : s.startsWith('-') ? -Infinity : Infinity;
  return null;
}

export function srsSettings(repos: UserRepos): PageResult {
  const current = repos.prefs.getDesiredRetention();
  return page('settings_srs.html', {
    current: current ?? DEFAULT_DESIRED_RETENTION,
    is_default: current === null,
    presets: RETENTION_PRESETS,
    saved: false,
  });
}

export function srsSettingsSave(repos: UserRepos, req: PageRequest): PageResult {
  const raw = req.form.get('retention') ?? '';
  const value = parseFloatLiteral(raw);
  if (value === null) throw badRequest(`retention must be a number, got ${literal(raw)}`);
  if (!(MIN_DESIRED_RETENTION <= value && value <= MAX_DESIRED_RETENTION)) throw badRequest(RETENTION_RANGE_MESSAGE);
  repos.prefs.setDesiredRetention(value);
  return page('settings_srs.html', {
    current: value,
    is_default: Math.abs(value - DEFAULT_DESIRED_RETENTION) < 1e-6,
    presets: RETENTION_PRESETS,
    saved: true,
  });
}
