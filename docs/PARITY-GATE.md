# Phase 0: the parity gate

Spec for phase 0 of `docs/CELLD-REWRITE.md` (sections 4, 5.2, the 4.3
pins). Four lanes, A to D, build in parallel with no shared files.
Nothing touches the TypeScript side; nothing changes what a production
user sees except the font bytes (A4).

## 0. Shared constants

Defined once in `tests/parity/harness/constants.py`; the oracles
import them from there. `prep/dev/parity_seed.py` (which cannot import
`tests/`) carries only the timezone, pinned equal by a registry test,
and takes every timestamp from the process clock:

- `PARITY_NOW = 2026-03-14T15:00:00Z` (Saturday, DST on in the parity tz)
- `PARITY_TZ = America/New_York`; the parity user's tz pref matches
- `PARITY_USER = parity@example.com`, display name `Parity`
- `PARITY_BUILD_ID = ce11d0000000` (token-shaped, used verbatim)
- `PARITY_INTERNAL_TOKEN = parity-internal-token` (local only)

Server env for a run: every pin above as its `PREP_*` variable,
`PREP_FREE_INFERENCE_{BASE_URL=<stub>/v1,API_KEY,MODEL}`, a scratch
`PREP_DB_PATH`, the e2e `PREP_KEY_ENCRYPTION_SECRET`, tailscale auth.

## A. Pins in the Python app (lane A)

### A1. One clock seam

`prep/infrastructure/clock.py`:

```python
class Clock(Protocol):
    def now(self) -> datetime              # aware UTC
class SystemClock: ...
class FixedClock:  def __init__(self, at: datetime): ...
def get_clock() -> Clock; def set_clock(c) -> None; def reset_clock() -> None
def now() -> datetime; def now_iso(timespec="auto") -> str; def unix() -> float
```

Process-wide provider, resolved lazily on first `get_clock()`:
`PREP_FAKE_NOW` set means `FixedClock`, else `SystemClock`. ISO-8601
with `Z` or an offset; naive means UTC; malformed raises `ValueError`
naming the variable. `reset_clock()` drops the cached provider for
tests. A fixed clock logs a boot WARNING.

All 28 sites in 16 files (`grep -rnE 'datetime\.now\(|time\.time\(' prep/`)
route through it. `prep/infrastructure/db.py: now()` delegates to
`clock.now_iso()`, its callers untouched. `prep/domain/srs.py` (2):
`now` becomes a required keyword on `schedule_review` and
`seed_state_from_ladder_step`, the domain imports no clock, callers in
`prep/study/repo.py` and the boot migration pass `clock.now()`,
`tests/domain/test_srs.py` updated. `time.time()` in `anon_cookie.py`
(3) and `anki_export.py` (1) -> `clock.unix()`. The other 20
`datetime.now(timezone.utc)` in 12 files -> `clock.now()`; existing
`now: datetime | None` parameters keep their signature and default
from the clock.

Tests, `tests/infrastructure/test_clock.py`: parse `Z`, offset, naive,
malformed; `SystemClock` is aware UTC; set/reset;
`test_no_direct_clock_calls`, a regex scan asserting the only file
under `prep/` containing `datetime.now(`, `utcnow(` or `time.time(` is
`clock.py`. That scan keeps the count at zero.

### A2. Seeded landing placeholder

`prep/web/index.py: _topic_placeholder()` returns
`TOPIC_PLACEHOLDERS[int(PREP_PLACEHOLDER_INDEX) % len]` when set, else
`random.choice`. Test in `tests/web/test_landing_instant.py`: index 0
and 14 (wraps) render the expected text; unset stays random.

### A3. Free-tier base URL

Already env-driven, no change: `PREP_FREE_INFERENCE_{BASE_URL,
API_KEY,MODEL}` in `prep/agent/selector.py`, non-streaming JSON via
`openai_compat.py`. The free tier funds every user without BYOK, so
the stub covers signed-in flows. BYOK has no base-URL override; a BYOK
key at the stub is not a phase 0 item.

### A4. Fonts: self-hosted

Decision: self-host under `static/fonts/`. Both families are SIL OFL 1.1
(undercasetype/Fraunces, JetBrains/JetBrainsMono), committed with
their `OFL.txt`. Self-hosting makes the bytes identical on both
servers with no interception: a plain browser on staging renders what
the gate measures. Harness routing would leave production on Google's
per-UA subsetted builds, unlike every golden.

Files: the variable `woff2` of each family, roman and italic, release
tags in `static/fonts/SOURCES.md`. `static/css/fonts.css` declares one
`@font-face` per file with the descriptors the Google CSS serves, no
`unicode-range`; `index.css` imports it first, outside the layers.
`base.html` drops the `preconnect` links and the Google stylesheet.
Fonts are NOT added to the service-worker precache (`pwa.py`
enumerates css and js only; unchanged contract).

Tests, `tests/web/test_fonts_pin.py`: no rendered page references a
Google font host; every `url(...)` in `fonts.css` is a committed file;
the families in `fonts.css` equal those in `tokens.css`;
`GET /static/fonts/<f>` is 200 `font/woff2`.

### A5. `PREP_PARITY_MODE`

`PREP_PARITY_MODE=1` (never in a deploy file; boot WARNING when set):

- `_clerk_bootstrap_context` returns both keys `None`, so `base.html`
  omits the ClerkJS tag and its bootstrap.
- `/docs` and `/redoc` pass their HTML through
  `prep/web/parity.py: strip_cross_origin_tags(html, host)`, removing
  `<script src>` and `<link href>` whose host is not the request host;
  the gate compares the empty vendor shells.
- `POST /_parity/seed` (C6), `GET /_parity/raise` (a deliberate 500,
  or 429 with `?status=429`), and `GET /_parity/reauth` and
  `GET /_parity/sign-out`, which render the two shells `GET /` and
  `GET /sign-out` serve only under a Clerk session state, with the
  same context.

Tests, `tests/web/test_parity_mode.py`: with the flag, landing,
dashboard, `/docs`, `/redoc` reference no other origin; without it,
ClerkJS (under a publishable key) and the redoc CDN are present;
`strip_cross_origin_tags` unit cases.

## B. The canned LLM (lane B)

`tests/parity/llm_stub.py`: a `ThreadingHTTPServer` on `127.0.0.1`,
standalone (`python -m tests.parity.llm_stub --port 8089 --fixtures
<dir> [--record]`) and as a session fixture `llm_stub` with
`base_url` (ends in `/v1`), `hold()`, `release()`, `latency(ms)`,
`requests` (keys seen), `reset()`.

- `POST /v1/chat/completions`. Key = sha256 of
  `json.dumps(body["messages"], sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)`; every other field is ignored so a model rename
  never invalidates the set.
- Fixture `<dir>/<key[:16]>.json`: `{"key", "messages", "body"}`, `body`
  the exact upstream response text, served verbatim with
  `content-type: application/json`, `content-length`, and fixed
  `Server` and `Date` headers (override `version_string`,
  `date_time_string`). Contract: same key, identical bytes and
  headers, every time.
- Miss: 404 `{"error": "no fixture for <key>"}` plus the request body
  written to `<dir>/missing/<key[:16]>.json`. CI never records.
- `--record` / `PARITY_LLM_RECORD=1`: a miss is forwarded to
  `PARITY_LLM_UPSTREAM_{BASE_URL,API_KEY,MODEL}`, stored as `body`,
  then served. Recording happens once, running the golden capture
  against the real free tier through the stub. A CI miss means a
  prompt stopped being deterministic: fix the prompt, never the key.
- Control (`POST` unless noted): `/_control/hold` (later requests
  block), `/_control/release`, `GET /_control/held` ->
  `{"count", "keys"}`, `/_control/latency` `{"ms"}`, `/_control/reset`.
  A held request waits on a `threading.Event`, answering 503 after
  120 s so a forgotten hold cannot hang a run.
- No streaming; the adapter never asks for it.

Tests, `tests/parity/test_llm_stub.py`: key stability; byte- and
header-stable replay; miss file; hold until release; latency; record
against an in-process fake upstream.

## C. The pixel harness (lane C)

### C1. Layout

```
tests/parity/
  conftest.py redproof.sh
  harness/constants.py contextspec.py capture.py compare.py registry.py
          runner.py server.py serve.py fixtures.py
          test_compare.py test_registry.py
  flows/__init__.py flows/<flow>.py ...
  test_flows_<flow>.py            (one browser file per flow)
  goldens/<flow>/<NN-label>@<scheme>.png
```

Dev deps added: `pillow`, `numpy` (`uv sync --group dev`).

### C2. Context

`contextspec.py: new_context(browser, scheme, *, service_workers)`:
393x852, `device_scale_factor=3`, the iOS UA from
`tests/e2e/conftest.py`, `is_mobile`, `has_touch`,
`reduced_motion="reduce"` (the stylesheet zeroes all motion under it),
`color_scheme` in `{"light", "dark"}`, `timezone_id=PARITY_TZ`,
`locale="en-US"`, `service_workers="block"`
unless the flow says `allow`. Every page gets
`page.clock.set_fixed_time(PARITY_NOW)`: `Date` pinned, timers real so
htmx polling runs. Same-origin requests carry the tailscale identity
headers for `PARITY_USER` via `ctx.route`; a Clerk target uses a
storage state instead.

### C3. Capture

`capture.py: shot(page, path, *, after_swap=None)` waits for
`document.fonts.ready`, then `_ANIMATIONS_SETTLED` imported from
`tests/e2e/flow_artifacts.py`, then `after_swap` if given, then
`page.screenshot(full_page=True, animations="disabled", caret="hide",
scale="device")`. `expect_after_swap(page)` installs a one-shot
`htmx:afterSwap` promise BEFORE the action that starts polling; every
polling screen is captured after its first swap.

### C4. Comparator

`compare.py: compare(golden, candidate, diff_out) -> Report`. Sizes
must match. A pixel FAILS when any RGB channel differs by more than
`CHANNEL_TOL = 2`. Pass iff `failing <= MAX_FAIL_RATIO * area`
(`0.0002`) AND no aligned 8x8 block holds more than `BLOCK_MAX_FAIL = 2`
failing pixels. The block rule is the plan's "scattered anti-aliasing
passes, a shifted glyph fails": a moved stem at DPR 3 puts 20+
failures in one block. Diff mask: the candidate dimmed, failing
pixels red, failing blocks outlined yellow, written to
`artifacts/parity/<flow>/<NN-label>@<scheme>.diff.png` on failure and
attached via `record_property`. `test_compare.py` uses synthetic
images: identical, AA noise, 0.01% scattered, a 3x9 shifted stem
(fails the block rule only), size mismatch.

### C5. Flow registry

```python
@dataclass(frozen=True)
class Flow:
    name: str
    phase: int
    seed: str
    covers: tuple[str, ...]
    service_workers: str = "block"
    schemes = ("light", "dark")
    steps: Callable[[FlowCtx], None]
```

`FlowCtx` carries `page`, `base_url`, `seed` (the seed response), `llm`
(stub handle), `shot(label, *, after_swap=None)`; shots number
themselves in call order. One module per flow registers with
`@flow(...)`. `covers` names templates and partial states;
`test_registry.py` parses `templates/` and asserts every page
template, every partial, every `_status` literal of the three progress
partials, the nine `transform_diff_card` states, the three error
statuses, both reauth cookie states, and the sign-out and device-wipe
dialogs appear in at least one flow.

| phase | flows (both schemes each) |
| --- | --- |
| 1 | `landing` (instant on/off), `privacy`, `errors` (404, 429, 500 via `/_parity/raise`), `reauth` (both cookie states), `dashboard` (empty; populated with snoozed, pinned, badge, PWA nudge), `deck` (srs with suspended; empty; trivia; menus, overflow, pin form, duration sheet, delete dialog), `deck-new` (chooser, srs, trivia), `question` (new, edit), `settings` (all five pages, agent none/connected, api with a token, notify prefs and log), `sign-out` (interstitial, device-wipe dialog) |
| 3 | `study` (five card types; right, wrong, idk; done; snooze), `trivia` (deep link, session, done), `offline` (SW allowed: shell, study, dashboard) |
| 4 | `plan` (every status incl. round 2 and `gone`; planning held), `transform` (three scopes, every status, nine diff-card states, improve dialog), `reorganize`, `trivia-generating` (held, applying, done), `grading` (held), `badge` (after first swap) |
| 5 | `import` (csv, prepdeck, anki, each with an error state), `export`, `split` |

About 70 screens. `PARITY_PHASE=n` runs phases `<= n`; `PARITY_FLOWS`
globs names; `PARITY_SCHEME` picks one scheme.

Target-bound states, phases 1 and 3: the landing renders the instant
hero only where the provider exposes a sign-in URL, so a tailscale
target captures the marketing hero and a Clerk target the instant
one; `settings_account.html` is a 404 outside Clerk; the reauth shell
and the sign-out interstitial come from the `/_parity/*` routes, and
the device-wipe choice is opened from `offline/wipe.js` directly, the
row that opens it being provider-gated. The offline flow runs with
service workers blocked: a controlling worker re-issues the snapshot
fetch without the injected identity header, trips the owner guard
and wipes the stores. Free-text study answers book the LLM grader,
so the short and code cards take the `idk` path. The trivia session
is opened with an explicit `?cards=` queue; a fresh session draws its
order at random. `navigator.storage.estimate()` and `persisted()` are
pinned in the context, like the clock. The two vendor doc shells are
blank under the flag, so no CSS knob can redden a shot of them; they
are held by the contracts corpus (D) as DOM pairs instead.

### C6. Seed mechanism

Decision: an env-gated endpoint, not a sqlite file. A committed sqlite
freezes a schema migrations rewrite and cannot reach staging. The
endpoint runs on both targets through the repositories, so it
survives migrations.

`POST /_parity/seed`, registered only under `PREP_PARITY_MODE=1`,
`X-Internal-Token` checked by `_require_internal_token`
(`prep/agent/routes.py`), body `{"user": <login>, "profile": <name>}`.
It deletes every user-scoped row of that login (tables from
`prep/auth/merge.py: discover_user_scoped_tables`), recreates the user
with tz `PARITY_TZ`, inserts the profile. Profiles live in
`prep/dev/parity_seed.py` beside `preview.py`, timestamps absolute
from `PARITY_NOW`. Response: the ids the flows need. Profiles:
`empty`; `reader` (two srs decks, one trivia, one empty, a suspended
card, a snoozed session, a pinned deck, unseen notifications, a PAT
with a fixed plaintext); `study` (a session mid-way, every card type
due in its own wall-clock hour: the queue shuffles ties within an
hour); `workflows` (a plan, a transform per scope, a trivia
generation, each in the requested state; phase 4, needs a worker
since the pages query Temporal) and `caps` (an anonymous account at
every cap) are not implemented yet. Anonymous flows mint their cookie
through `/api/instant/generate`.

### C7. Modes and the server

`PARITY_MODE=golden` writes `goldens/<flow>/<NN-label>@<scheme>.png`
and passes; default mode compares and fails per shot. `PARITY_BASE_URL`
unset boots a local uvicorn through `harness/server.py` (the
`LocalOfflineServer` shape, section 0 env, the stub's URL); set, the
harness seeds and captures against it unchanged. Each
`test_flows_<flow>.py` holds one `browser`-marked test per scheme and
re-seeds at the start of the flow; files run one per invocation.

## D. The domain oracles (lane D)

Each extractor is `python -m tests.parity.oracles.<name>`, writing
under `tests/fixtures/parity/<name>/`.
`tests/parity/oracles/test_oracles.py` re-runs every extractor in
memory and asserts equality with the committed corpus, so no corpus
drifts from Python silently; the FSRS perturbation goes red here.
Extractors run under `pin_clock()`: `PREP_FAKE_NOW=PARITY_NOW` and the
process clock re-resolved from it. The DB-backed extractors seed the
`reader` profile from `prep/dev/parity_seed.py`, the same rows the
pixel flows see.

- `fsrs`: fuzz off by pre-populating `srs._SCHEDULER_CACHE[round(r, 3)]`
  with `Scheduler(desired_retention=r, enable_fuzzing=False)` for each
  retention used; `prep/` untouched. Cases from
  `random.Random(20260314)`: start states = fresh, ladder seeds 1..5,
  stabilities at each `step_for_stability` threshold plus or minus
  1e-6; sequences of 1..8 reviews, verdicts `{RIGHT, WRONG}`, elapsed
  in `{0, 1m, 10m, 1h, 1d, 3d, 7d, 30d, 365d}`; retentions `{0.5, 0.70,
  0.80, 0.90, 0.95, 0.97, 0.99}` (both clamps). At least 5,000
  transitions. Row: input state, verdict, `now`, retention; output
  `stability`, `difficulty` (`repr` floats, compared at 1e-9),
  `fsrs_state`, `last_review`, `next_due` (exact ISO), `interval_seconds`,
  `step_bucket`. Header: py-fsrs version and parameters.
- `grading`: every branch of `grade` (mcq right/wrong/case; multi
  exact, partial, extra, missing, with the `sorted()` list repr in the
  feedback; short with regex match, miss, no pattern, invalid; code;
  `idk`; empty), `match_regex` (None, invalid, match, miss),
  `validate_regex_update` (non-str, empty, over `MAX_REGEX_LEN`,
  invalid, misses expected, misses prior, accepted).
- `markdown`: the 60 `cases.json` inputs through the registered filter,
  `{id, input, expected}` only; the test asserts equality with
  `cases.json`'s `expected`, so `js_expected` retires without drift.
- `merge`: scratch DB; anonymous account with a row in every table
  `discover_user_scoped_tables` returns (asserted), both ledgers,
  `desired_retention` and `editor_input_mode` set, two slug collisions
  (numbered suffix, then random tail with `prep.auth.merge.secrets`
  patched to a seeded generator), target at every cap. Corpus:
  `before.json` and `after.json` (rows per table, the `account_merges`
  row, `MergeResult`), `previous_ids` from `/api/offline/snapshot`.
- `offline`: TestClient pairs `{name, request, response}`: snapshot;
  new card created and rejected (cap, bad deck); a review per outcome;
  reviews rejected (unknown card, missing client id, bad and future
  timestamp); the batch replayed; 422 over-cap (101 cards, 501
  reviews); 422 malformed.
- `contracts`: every route under `/api/study`, `/api/dashboard`,
  `/api/offline`, `/api/instant`, `/notify`,
  `/api/active-workflows-badge`, `/api/v1`, `/mcp`, enumerated from
  `app.routes` (none missing, asserted), recorded against the
  `reader` profile; the anonymous `Set-Cookie` lifecycle (mint on
  instant generate, refresh after `REFRESH_AFTER_SECONDS` with the
  clock advanced, clear on `/forget-device` and on a bad signature);
  MCP `tools/list` and one call per tool (17); `openapi.json`; the
  `/docs` and `/redoc` shells under `PREP_PARITY_MODE=1`, compared as
  DOM. A `VOLATILE` map of JSON paths compared by regex covers the PAT
  secret.
- DOM differ, `tests/parity/dom_diff.py: dom_diff(a, b) -> list[Diff]`
  (stdlib `html.parser`, `convert_charrefs=True`, void elements
  closed): equal element tree; attribute sets with decoded values
  (`disabled` equals `disabled=""`); text decoded and
  whitespace-collapsed except inside `pre`, `textarea`, `script`,
  `style`; `<script type="application/json">` compared as parsed
  JSON; comments and doctype ignored; the ordered `(tag, src|href)`
  list of `script` and `link` must match. Each `Diff` carries a
  CSS-like path. `test_dom_diff.py`: `&#34;` vs `&quot;`, attribute
  order, trailing newline, `tojson` separators, a changed `data-*`
  (reported at its path), a reordered `<link>`.
- Golden renderer, `oracles/render_templates.py` ->
  `tests/fixtures/parity/html/<template>@<context>.html`, rendering
  `templates.env.get_template(name).render(base_context() | ctx)` with
  the nine context-processor names supplied explicitly (`deck_display`
  as a dict lookup, `clerk_*` None) and a fake `request` with
  `scope={"root_path": ""}`. Contexts in `oracles/contexts.py`: every
  page template and partial at least once; `plan_progress` per
  `_status` (rounds 1 and 2, `_generated` set and unset, `error`);
  `transform_progress` per `progress.status` for the three scopes,
  `group_by_deck` both ways, every list kind with overflow;
  `trivia_generating_progress` generating, applying, done,
  error; `index.html` and `deck.html` with a deck display name
  `</script><script>alert(1)</script>`. A coverage test asserts every
  status literal in the three partials appears in a context.

## E. The red proof

Three env knobs, each flipping exactly one check:

- `PARITY_PERTURB_CSS=1` (C): `capture.py` adds
  `main{transform:translateY(1px)}` via `page.add_style_tag`, a shift
  that trips the block rule without changing the page size. Red: every
  `test_flows_*` shot. Green: oracles, domdiff.
- `PARITY_PERTURB_FSRS=1` (D): the fsrs extractor bumps `w[0]` by
  `1e-6`. Red: `test_oracles[fsrs]` only.
- `PARITY_PERTURB_DOM=1` (D): `test_oracles[html]` rewrites one
  `data-qid` in the candidate before diffing. Red: `test_oracles[html]`
  only, path named.

`tests/parity/redproof.sh` (C) runs the three with `--junitxml` (the
pixel files one per invocation) and exits 0 only when the failing ids
are exactly the expected sets.

## F. Lanes, acceptance, commands

| lane | owns | acceptance | tests to run |
| --- | --- | --- | --- |
| A | `prep/**` except `prep/dev/parity_seed.py`; `templates/base.html`; `static/fonts/**`, `static/css/fonts.css`, `static/css/index.css`; the five test files named in A | clock scan at 0; A tests green; `make test` green once at the end | `.venv/bin/pytest tests/infrastructure tests/web/test_fonts_pin.py tests/web/test_parity_mode.py tests/web/test_landing_instant.py tests/domain -q`, then `make test` |
| B | `tests/parity/llm_stub.py`, `tests/parity/test_llm_stub.py`, `tests/fixtures/parity/llm/**` | byte-stable replay, hold, record, no network | `.venv/bin/pytest tests/parity/test_llm_stub.py -q` |
| C | `tests/parity/**` except B and D files; `prep/dev/parity_seed.py` plus its `register` line in `prep/app.py`; `pyproject.toml` dev deps | registry and comparator tests green; golden then compare mode pass locally for phases 1 and 3; `redproof.sh` exits 0 once D lands | `.venv/bin/pytest tests/parity/harness -q`; browser: `PARITY_PHASE=3 .venv/bin/pytest tests/parity/test_flows_<flow>.py -q`, one file per invocation |
| D | `tests/parity/oracles/**`, `tests/parity/dom_diff.py`, `tests/parity/test_dom_diff.py`, `tests/fixtures/parity/**` except `llm/` | every corpus committed and reproducible; coverage tests green; both D knobs red as E states | `.venv/bin/pytest tests/parity/test_dom_diff.py tests/parity/oracles/test_oracles.py -q` |

The whole gate, locally: `.venv/bin/pytest tests/parity/harness tests/parity/test_dom_diff.py tests/parity/test_llm_stub.py tests/parity/oracles/test_oracles.py -q`, then `PARITY_PHASE=3 .venv/bin/pytest tests/parity/test_flows_<flow>.py -q` per flow file, then `tests/parity/redproof.sh`.

Out of scope: anything under `worker/`; staging capture and the Clerk
authorized-party registration; a BYOK key at the stub; git LFS for
goldens; any visual change beyond the font bytes.
