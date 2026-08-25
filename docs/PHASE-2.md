# Phase 2: the domain

Spec for phase 2 of `docs/CELLD-REWRITE.md` (2.2, 2.5, 4.2, 5.4, risk 1,
decision 7.5). Four lanes, A to D, no shared files; E is the tests and
commands, F the boundary. Pure TypeScript under `worker/domain/`, vitest,
no I/O. Gate: every corpus under `tests/fixtures/parity/` passes against
the TypeScript implementation.

## 0. Layout and rules

```
worker/domain/
  index.ts            re-exports; the integrator adds one line per lane at the end
  py.ts               A: pyRound(x, nd) (half-even), pyStrip, codePoints(s), isoUtc(date),
                      parseIso(s): Python aware-UTC isoformat, naive parses as UTC
  fsrs/               A: index.ts scheduler.ts fuzz.ts
  grading/            B: index.ts regex.ts pyrepr.ts client.ts
  markdown/           C: index.ts (+ block.ts inline.ts url.ts)
  merge.ts limits.ts trivia.ts anonCookie.ts instant/{cards,limiter,ip}.ts   D
worker/tests/domain/  one test file per module (E)
worker/scripts/build-domain.mjs   C
```

`py.ts` is lane A's first commit; B and D code against those signatures
and may create the file with exactly them if A has not landed. Every lane:

- Pure: no clock, random, env, fetch, crypto, storage; time and randomness
  are arguments. No npm runtime dependency. The layering test stays green.
- Where Python raises, TypeScript throws a named `Error` subclass; tests
  assert the class, never the message.
- Corpora are read-only. A gap is closed by adding a case to the Python
  extractor or fixture file, rerunning `.venv/bin/python -m
  tests.parity.oracles.<name>` and `.venv/bin/pytest
  tests/parity/oracles/test_oracles.py -q -k <name>`, and committing both.
  Never hand-write an expectation.
- TDD per behavior; run only the files E names. Public repo, terse
  comments, no em dashes; the idk feedback string that contains one is
  parity data and stays byte-exact.

### 0.1 Client twins (B and C)

`domain/markdown` and `domain/grading` also run in the browser. The
TypeScript is the source; `static/js/study/markdown.js` and
`static/js/offline/grader.js` become generated, committed files:
`scripts/build-domain.mjs` runs esbuild (`bundle`, `format: 'esm'`,
`target: 'es2022'`, no minify, a `do not edit` banner) on
`domain/markdown/index.ts` and `domain/grading/client.ts`. Same paths and
export names as today (`markdownHTML`; `grade`, `matchRegex`,
`MAX_REGEX_LEN`). `npm run build:domain` regenerates and `npm run build`
runs it first; `bundles.test.ts` rebuilds in memory and asserts byte
equality. Lane C changes one line of
`runtime/adapters/nunjucks/shims.ts`: the `markdown` filter imports
`../../../domain/markdown`.

## A. `domain/fsrs` (lane A)

Port of `prep/domain/srs.py`.

```ts
export type Verdict = 'right' | 'wrong';
export const FsrsState = { Learning: 1, Review: 2, Relearning: 3 } as const;
export interface CardSRSState { stability: number | null; difficulty: number | null;
  fsrsState: 1 | 2 | 3; lastReview: Date | null }
export interface ScheduledReview { state: CardSRSState; nextDue: Date; intervalSeconds: number; stepBucket: number }
export const DEFAULT_DESIRED_RETENTION = 0.9, MIN_DESIRED_RETENTION = 0.7, MAX_DESIRED_RETENTION = 0.97, TERMINAL_STEP = 5;
export function freshState(): CardSRSState;
export function stepForStability(s: number | null): number;  // null or <1: 0, <3: 1, <7: 2, <14: 3, <30: 4, else 5
export function seedStateFromLadderStep(step: number, now: Date): CardSRSState;  // srs.py's table; >5 is 30
export type Fuzz = false | { random: () => number };
export function scheduleReview(state: CardSRSState, verdict: Verdict, now: Date,
  opts: { desiredRetention?: number | null; fuzz: Fuzz }): ScheduledReview;
export class RelearningStepMissing extends Error {}
```

- `right` is Good (3), `wrong` is Again (1); Hard and Easy are unreachable.
  Retention: null means 0.9; clamp to [0.7, 0.97]; `pyRound(x, 3)` is the
  scheduler's retention. `fsrsState` 0 or missing means Learning.
- The scheduler is py-fsrs 6.3.2's with its default 21 parameters (the corpus
  header), learning steps 60s and 600s, relearning 600s, maximum interval 36500 days. `step` is never persisted:
  a Learning card enters at step 0 on every review, and a Relearning input
  throws `RelearningStepMissing` (py-fsrs's `assert card.step is not
  None`; 2117 corpus rows).
- Elapsed days: whole days of `now - lastReview`, floored (`timedelta.days`).
  Branches and formulas are transcribed from `review_card` in
  `.venv/lib/python3.11/site-packages/fsrs/scheduler.py`: the short-term
  branch under one elapsed day, the forget cap `S / e^(w17 w18)`, the mean
  reversion to the unclamped Easy initial difficulty, `pyRound` for the
  interval (clamp [1, 36500]). `nextDue = now + interval`; `intervalSeconds =
  max(0, trunc(seconds))`; `stepBucket = stepForStability(S)`;
  `lastReview = now`.

**ts-fsrs, measured on 5.4.1 before this spec:** it rounds to 8 decimals
in the forgetting curve, the interval modifier and every S and D update; `next_interval` is `Math.round`
(half-up; Python's `round` is half-even); its fuzz floor adds
`elapsed_days + 1` and draws from an Alea PRNG. Rounding at 1e-8 cannot
meet 1e-9, so the vendor escape (risk 1) is taken from the start:
`domain/fsrs/scheduler.ts` is a direct port of py-fsrs 6.3.2's math, its
license notice in the file header; ts-fsrs is not a dependency. Not
acceptable: ts-fsrs plus a correction pass, a tolerance above 1e-9, slack
on `nextDue`, or skipping rows. A failing row is fixed in the port.

**Fuzz (`fuzz.ts`, decision 7.5: on in production).** Port
`_get_fuzzed_interval`, applied only to a Review result: `days` is the
whole-day part of the unfuzzed interval; below 2.5 unchanged; `delta = 1 +
Σ factor · max(min(days, end) − start, 0)` over ranges (2.5, 7, 0.15),
(7, 20, 0.1), (20, ∞, 0.05); `min = max(2, pyRound(days − delta))`, `max =
min(pyRound(days + delta), 36500)`, `min = min(min, max)`; result
`min(pyRound(random() · (max − min + 1) + min), 36500)` days. Production
passes `{ random: Math.random }`.

## B. `domain/grading` (lane B)

One module replaces `prep/domain/grading.py` and `grader.js`.

```ts
export const MAX_REGEX_LEN = 500;
export type Question = Record<string, unknown>;
export interface GradeResult { result: Verdict; feedback: string; model_answer_summary: string }
export class UnsupportedQuestionType extends Error {}
export function grade(question: Question, userAnswer: string, idk?: boolean): GradeResult;
export function gradeOffline(card: Question | null, userAnswer: unknown, idk?: boolean): { verdict: Verdict } | null;
export function matchRegex(pattern: unknown, given: unknown): boolean | null;
export function validateRegexUpdate(pattern: unknown, expectedLiteral: string | null, priorGiven?: string | null): string | null;
```

- idk: `wrong`, feedback `Marked as 'I don't know' <U+2014> see again
  soon.` (literal em dash), summary the first 400 code points of `answer` (`''` when missing). mcq: `pyStrip` both sides,
  case-sensitive; `Correct.` / `Wrong choice.`; summary the raw answer.
  Any other type without idk throws `UnsupportedQuestionType`.
- multi: `toSet(JSON.parse(x))` on `userAnswer` (empty string is the empty
  set) and `answer`, with Python `set()` semantics: array gives elements,
  string its code points, object its keys, anything else, or a
  non-scalar element, is a `TypeError`. A `SyntaxError` or `TypeError` on either side empties BOTH sets, so a
  broken pair grades right. Feedback `Expected: ${repr}; you picked:
  ${repr}.` and the summary use `pyrepr.ts`: Python `sorted()` (by code
  point; mixed types throw `GradingError`) as Python's list `repr` (quote
  choice, escapes, `True/False/None`, floats keeping `.0`), unit-tested.
- `matchRegex`: non-string or empty gives null; over 500 null; `given =
  pyStrip(String(given ?? ''))`; shorthand `\w \W \b \B \d \D` with
  non-ASCII in the pattern or `given` gives null. Translations: `(?P<n>`
  to `(?<n>`, `(?P=n)` to `\k<n>`, and
  one leading inline-flag group whose letters are a subset of `is` is
  removed (both flags are always on). Then `new RegExp(p, 'isu')` as a
  validity probe and `new RegExp('^(?:' + p + ')$', 'isu').test(given)`; any
  throw gives null. Rejected by the `u` engine, each pinned to null by a
  unit test: `\A`, `\Z`, other inline flags, scoped `(?i:…)`, `(?#…)`,
  possessive and atomic groups, conditionals, `{,n}`, `\N{…}`, unknown
  escapes. Accepted divergence: non-ASCII case folding.
- `validateRegexUpdate`: non-string or empty null; `pyStrip`; empty or
  over 500 null; the same translation and probe (no shorthand rule); must
  fullmatch `pyStrip(expectedLiteral ?? '')` and, when `priorGiven` is not
  null, `pyStrip(priorGiven)`; returns the stripped pattern.
- `gradeOffline`: idk `wrong`; mcq and multi the verdict only; short null
  when `matchRegex` is null, else the verdict; anything else null.
  `client.ts` exports `{ grade: gradeOffline, matchRegex, MAX_REGEX_LEN }`.
  In `grader_cases.json`, `short-regex-inline-flag-diverges` becomes
  `expected: {verdict: 'right'}` without `expected_py`.

## C. `domain/markdown` (lane C)

`markdownHTML(text: string | null | undefined): string`, `''` for empty.
The target is mistune 3.3.4 as configured in `prep/app.py` (`escape=True`,
`hard_wrap=False`, `strikethrough`, `table`, default `HTMLRenderer`), read
from the venv: its block and inline rules, `escape` (`& < > "`, not `'`),
`safe_url` (`http: https: mailto:` and the image `data:` set, else
`#harmful-link`) and `escape_url` (`quote` with its safe set, so a backtick
becomes `%60`). Close the nine `js_expected` divergences by reproducing the
server output: pipe tables (two-space cell indent, `style="text-align:…"`
when aligned), harmful links, images, setext headings, backslash escapes,
nested tight lists (`<li>a<ul>…</ul>\n</li>`), lazy list and blockquote
continuations, code spans inside link URLs. Delete every `js_expected`
from `tests/fixtures/markdown/cases.json`; the browser suite then compares
against `expected` (not run this phase).

Add cases per the section 0 procedure (plus `.venv/bin/pytest
tests/web/test_markdown_parity_fixtures.py -q`) for at least: an aligned
table, a loose list, an indented code block, a `mailto:` link, an escaped backtick, a `data:image/png;` image,
an autolink. The renderer is a block-then-inline parser of the subset,
not regex passes; nesting depth is capped at 32 and degrades to escaped
text; rendering never throws.

Safety (`markdown.xss.test.ts`): for the corpus inputs and a hostile list
(`<script>`, `<img onerror>`, `javascript:` and `data:text/html` links and
images, entity smuggling, `"` inside a URL, NUL, unterminated fences, 10k
nested quotes) the output matches a whitelist of the tags mistune emits
for this subset and the attributes `href src alt class start style`,
contains no `<script`, ` on[a-z]+=` or `javascript:`, and every input `<`
is `&lt;`.

## D. merge, limits, trivia, instant, cookie (lane D)

**`merge.ts`**, policy as data plus pure row mapping; transactions, audit
rows, `PRAGMA` discovery and the leftover assertion are the cell's:

```ts
export const POLICY: readonly TableRule[];   // prep/auth/merge.py, in order
export const CARRIED_USER_COLUMNS, DROPPED_USER_COLUMNS, NUMBERED_SUFFIXES = [2, 100], SUFFIX_BYTES = 3;
export function applyRule(rule, rows: Row[], anon, target): { rows: Row[]; moved: number; dropped: number };
export function decollideDeckSlugs(anonDecks: Row[], targetDecks: Row[], randomHex: (bytes: number) => string): Row[];
export function mergeRows(before: Snapshot, anon, target, randomHex): { after: Snapshot; counts };
export function precheck(anon: Row | null, target: Row | null, sameUser: boolean): MergeResult | null;
export function previousUserIds(merges: Row[], target): string[];   // status completed, by id
```

`applyRule`: DELETE removes the anon rows; REASSIGN rewrites the column;
REASSIGN_DROP_CONFLICTS first drops anon rows whose `conflictKey` value
exists among the target's. `carryPreferences(anon, target)` is COPY-IF-NULL.
`decollideDeckSlugs` renames anon decks in `id` order whose `name` is in
the target's: first free `${name}-${n}`, n = 2..100, against the union of
both namespaces (updated as renames land), then `${name}-${randomHex(3)}`
until free. `mergeRows` runs decollide, POLICY in order, prefs; counts
omit zeros; keys `table`, `table.dropped`, `users.<col>`. `precheck`
mirrors `merge_anonymous_into`'s four refusals and their `resolved` flags.

**`limits.ts`**: `ANON_MAX_DECKS = 5`, `ANON_MAX_QUESTIONS = 200`,
`RowCapReached`, `assertUnderRowCap({ isAnonymous: boolean | null, decks,
questions }, { newDecks = 0, newQuestions = 0 })`: a non-anonymous or
missing user returns; anonymous over either cap throws with the exact
message `guest account limit reached: 5 decks, 200 cards. Create an
account to add more.`

**`trivia.ts`**: `parseCardIds`, `parseDone`, `formatDone`,
`flipDoneVerdict` as `prep/trivia/session_state.py`. Accepted
divergences: digits are ASCII; ids above `MAX_SAFE_INTEGER` are dropped.

**`instant/cards.ts`**: the six constants of `prep/instant/service.py`;
`sanitizeTopic(raw: unknown)` (non-string null; over 1000 code points
null; `\t \r \n` to space; drop `\p{Cc}`; `pyStrip`; empty or over 500
null); `displayNameFor` (collapse `\s+`, strip, first 60 code points,
strip); `buildPrompt`; `parseQaPairs` (`prep/domain/qa_extract.py`: strip, drop a leading
```` ```(json)? ```` and trailing fence, first `[` to last `]`, `JSON.parse`,
non-array throws `QaParseError`); `extractCards` (`_extract_cards`, `r`
through `validateRegexUpdate` only when truthy, fewer than 3 throws
`DegenerateOutput`). Lengths are code points.

**`instant/limiter.ts`**: the constants and defaults of
`prep/instant/repo.py` (`DEFAULT_LIMITS`, `SPEND_OUTCOMES`,
`TERMINAL_OUTCOMES`), `retryAfter(at, createdAtIso | null, windowS)`
(missing or unparseable gives the window, else `max(1, ceil(windowS −
elapsed))`), and `checkWindows(rows: GenerationRow[], { ip, userId: string
| null, userIsAnonymous: boolean | null, at }, limits = DEFAULT_LIMITS):
Refusal | null` in `check_and_reserve`'s order: burst (every outcome,
same ip, within `burstWindowS`; count ≥ limit gives `minute` with
`retryAfter(newest)`); per-ip day (spend outcomes; ≥ limit gives `day` with
`retryAfter` of the row at index `n − limit` ascending); per-user day when
`userId` is set (anonymous or missing user 3, else 20); global minute ≥ 4
and global day ≥ 200 give `busy`; null admits. Instants are compared
parsed.

**`instant/ip.ts`**: `limiterBucket(value: string)`: empty or unparseable
gives `unresolved`; IPv4 (strict dotted quad) verbatim; IPv6: IPv4-mapped
gives the IPv4, else the /64 network as Python prints it (RFC 5952 compression of the address with the low 64 bits
zeroed, plus `/64`); scoped and bracketed forms give the sentinel
(accepted divergence). Header selection is the router's.

**`anonCookie.ts`**: the constants of `prep/auth/anon_cookie.py`. The MAC
is never computed here: phase 3's `Signer` port (`hmacSha256(bytes):
Promise<Uint8Array>`) supplies it, so every function is sync over bytes:
`externalIdFromBytes`, `idBytes` (throws on bad prefix or length),
`cookiePayload(externalId, issuedAt)` = `v1.<b64u(id)>.<iat>`,
`assembleCookie(payload, mac)` = payload + `.` + b64u of the first 16 MAC
bytes, `parseCookie(raw)` (ASCII only, four parts, version `v1`, id decodes
to 16 bytes, `iat` under Python `int()`'s grammar; else null),
`verifyCookie(parsed, mac, now)` (constant-time tag compare; `iat > now +
60` or `iat < now − 15552000` null), `needsRefresh(cookie, now)`. Base64url
is unpadded on encode and strict on decode. HKDF and `Set-Cookie`
belong to phase 3.

## E. Oracle tests

Corpus paths are under `tests/fixtures/parity/`.

- **fsrs**, `fsrs.oracle.test.ts`, oracle `parity/fsrs/corpus.json`: every review replayed from its `input`, fuzz off: S and D within 1e-9 absolute; state, `last_review`, `next_due`, `interval_seconds`, `step_bucket` exact; `error` rows throw `RelearningStepMissing`; 5640 transitions and 2117 throws counted.
- **fsrs**, `fsrs.fuzz.test.ts`, oracle the corpus, fuzz `{ random: seeded LCG }`: every Review-result transition with unfuzzed interval ≥ 3 days: whole days within the `[min, max]` of an independent copy of the range formula kept in the test; S, D, state equal the fuzz-off output; `random` 0 gives min, 0.999999 gives max; 64 draws on a 30-day interval yield ≥ 3 values; under 2.5 days identical to fuzz off.
- **grading**, `grading.oracle.test.ts`, `grading.regex.test.ts`, oracle `parity/grading/corpus.json`, `tests/offline/fixtures/grader_cases.json`: `grade` rows deep-equal `result` or throw `UnsupportedQuestionType` on `error`; `match_regex` and `validate_regex_update` rows exact; all 46 grader cases `module[fn](...args)` deep-equal `expected`; the named rejections, translations and pyrepr cases.
- **markdown**, `markdown.oracle.test.ts`, `markdown.xss.test.ts`, oracle `parity/markdown/corpus.json`: every case byte-equal to `expected`; section C's safety assertions.
- **merge**, `merge.oracle.test.ts`, oracle `parity/merge/{before,after}.json`: `mergeRows(before)` with `randomHex` stubbed to `fd58dd` (called once, with 3): rows per table and user equal `after.tables` as multisets; `users[target]`, counts (`after.result.counts` and the parsed `account_merges[0].counts`), `target_deck_slugs`, `previousUserIds` equal; the `precheck` table.
- **limits, trivia, limiter**, `limits.test.ts`, `trivia.test.ts`, `limiter.test.ts`, oracle none: n = limit − 1 admits and n = limit refuses per window; `retryAfter` ceil, floor 1, missing gives window; `failed_free` counts only for burst; burst wins over day; the three user limits; the docstring examples of `session_state.py`; the cap message byte-exact.
- **instant, ip, trivia, cookie**, `instant.oracle.test.ts`, `ip.oracle.test.ts`, `trivia.oracle.test.ts`, `anonCookie.oracle.test.ts`, oracle Python via `tests/pyoracle.ts`: input tables through `sanitize_topic`, `display_name_for`, `build_prompt`, `parse_qa_pairs`, `ipaddress` (20 addresses), the trivia helpers, `mint_cookie` / `verify_cookie` under `PREP_ANON_COOKIE_SECRET = "22" * 32` with explicit `issued_at` / `now` (HMAC via `node:crypto` in the test); exact, every rejection branch included.
- **twins**, `bundles.test.ts`, oracle the committed `static/js/{study/markdown,offline/grader}.js`: byte-equal to a fresh esbuild output.

Commands: each lane runs `cd worker && npx vitest run <its test files>
tests/layering.test.ts` (A `tests/domain/fsrs`; B `tests/domain/grading
tests/domain/bundles.test.ts`; C `tests/domain/markdown
tests/domain/bundles.test.ts`; D `tests/domain/{merge,limits,trivia,instant,ip,limiter,anonCookie}`)
and `npm run typecheck` at the end. Integration gate: `cd worker && npx
vitest run tests/domain tests/layering.test.ts && npm run typecheck`, then
`.venv/bin/pytest tests/parity/oracles/test_oracles.py -q -k "fsrs or
grading or markdown or merge"` and `.venv/bin/pytest
tests/web/test_markdown_parity_fixtures.py tests/offline/test_parity_fixtures.py
-q`. No browser, pixel or staging runs in this phase.

## F. Out of scope

Any I/O: cells, SQL, HTTP, WebCrypto, env, clocks, `Set-Cookie`,
repositories, `app/` use cases and ports, workflows, HKDF, the identity
providers, persisting FSRS `step` or changing the Relearning throw (phase 3
decides both against this corpus), template and shim changes beyond the
one import line, the browser and pixel suites.
