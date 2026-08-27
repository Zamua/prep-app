# Phase 5: the long tail

Spec for phase 5 of `docs/CELLD-REWRITE.md` (5.7). Four lanes. Nothing
here changes what a user sees; nothing touches the Python tree except to
extract corpora and goldens.

## 0. What is left, exactly

`worker/tests/routeTable.test.ts` already carries the gap as data. Nine
Python routes sit in `OUT_OF_SCOPE` marked `phase 5`:

| route | lane |
| --- | --- |
| `GET/POST /decks/import-csv` | A |
| `GET/POST /decks/import-prepdeck` | A |
| `GET/POST /decks/import-anki` | A |
| `GET /deck/{name}/export.prepdeck` | A |
| `GET /deck/{name}/export.apkg` | A |
| `GET /metrics` | B |

Plus two MCP tools answering `APKG_PENDING` today
(`prep_export_deck_apkg`, `prep_import_apkg`, `app/api/mcp.ts:242`), and
the pixel registry's phase-5 row (`import`, `export`, `split`).

`GET /deck/{name}/export.csv`, the export hub `GET /deck/{name}/export`,
`GET/POST /deck/{name}/split` and the CSV codec (`app/api/deckIo.ts`,
trivia preamble included) landed in phase 3: re-gated here, not
rewritten. When the phase ends, `OUT_OF_SCOPE` holds exactly the two
decision-7.6 debug routes, and the test asserts that.

## A. Import and export

### A1. Placement

Layering stays enforced. The pure half lives in `app/`, the WASM and zip
halves behind ports in `runtime/adapters/`.

- `app/decks/anki.ts`: the note-to-question mapping from
  `prep/decks/anki.py`. `_strip_html` (the `<br>` / block-end / media /
  tag / entity passes and the blank-line collapse), the `\x1f` field
  split, first field to prompt, remaining non-empty fields joined by a
  blank line to answer, the `{{c\d+::` cloze skip, the per-note error
  strings verbatim, prompt dedup, `AnkiImportOutcome`.
- `app/decks/ankiExport.ts`: `_build_question_body` (choices on the
  front for mcq/multi, the Explanation / Rubric / Skeleton / Topic
  sections, the newline-to-`<br>` pass), `_col_payload`, and the `col` /
  `notes` / `cards` row payloads. No sqlite, no zip.
- `app/decks/archive.ts`: `.prepdeck` from `prep/decks/archive.py`.
  `FORMAT_VERSION = 1`, the three column tuples, `meta.json`, prompt as
  the join key, the version-newer refusal, the four-entry read.
- Ports on `app/ports.ts`: `ApkgReader { notes(blob): { id, flds }[] }`,
  `ApkgWriter { build(col, notes, cards): Uint8Array }`,
  `ZipCodec { read, write }`.
- `runtime/adapters/apkg.ts`: `fflate` for the container, `sql.js` for
  the collection. sql.js loads through the module-import path with the
  browser glue (`sql-wasm-browser.js`) and its 658 KB `.wasm` sidecar;
  the node glue dies on `node:fs`. The compiled module is module-level
  state (per isolate, shared by every cell on the node); a `Database` is
  created and `close()`d inside one request, never module-level.

New deps: `sql.js` and `fflate`, both bundled, no CDN.

### A2. Size limits against the 128 MB heap

Python caps nothing: FastAPI spools an upload to disk and `sqlite3`
opens a temp file. A cell has 128 MB of heap and no disk, and holds the
upload, the inflated collection, and the sql.js linear memory at once.
Caps, enforced in `runtime/cells/routes/pages.ts` before any parsing:

| limit | value | what the user sees |
| --- | --- | --- |
| request body, `.apkg` | 8 MiB | 413, the importer page re-rendered with `error` = `That file is too large. The limit is 8 MB.` |
| request body, `.prepdeck` | 2 MiB | the same page, `The limit is 2 MB.` |
| request body, `.csv` | 1.5 MiB | the same page, `The limit is 1.5 MB.` |
| any single inflated zip entry | 32 MiB | 400, `error` = `That archive expands past 32 MB.`; read from the central directory before inflating, so a zip bomb never inflates |
| every inflated zip entry together | 32 MiB | the same 400; per-entry alone bounds nothing, because an archive may hold any number of entries and any number of them under one name |
| notes per `.apkg` import, rows per CSV | 5,000 | everything up to the cap is inserted (Python's partial-insert semantics already), and `outcome.errors` gains `stopped at 5,000 rows; split the file and import again` |
| `reviews.csv` rows per `.prepdeck` import | 50,000 | the same partial insert, `outcome.errors` gains `reviews.csv: stopped at 50,000 rows; ...`. Higher than the card cap because a card carries many reviews: a 2 MiB body of the narrowest rows a golden holds is about 39,000, so an honest archive never reaches it |
| `trivia_queue.csv` rows per `.prepdeck` import | 5,000 | the card cap, because a queue row names a card. Sorted first, so the lowest positions survive |
| questions per `.apkg` / `.prepdeck` export | 5,000 | 413 rendered from the export hub, `error` = `This deck is too large to export in this format.` |

`Content-Length` is checked first; a chunked body is counted while read
and aborted at the cap.

**A codec inflates only the entries it reads.** `zip.read` takes the
names its caller wants and returns `false` from the central-directory
filter for every other one, so `.apkg` inflates the collection and never
the media, and `.prepdeck` inflates its four sections and nothing else.
This is what `zf.read(name)` per entry already gave the reference for
free, and without it the per-entry ceiling is the only bound on a body
that can carry hundreds of entries.

**The export refusal is a rewrite-only page state.** The reference caps
no export, so `deck_export.html` gains `{% if error %}` with no
counterpart, no golden and no DOM comparison; `templates.test.ts` renders
it instead. The hub is re-rendered rather than replaced by the error page
because CSV has no ceiling, so the format that still works stays one tap
away.

**Measured, on a local celld node at `CELLD_V8_HEAP_LIMIT_MB=64`** (the
gate is half the 128 MB isolate, the renderer keeps the other half). Each
row is the largest workload that answered; the row above the line for CSV
refused with the isolate over its limit.

| workload | body | inflated | rows | at 64 MiB |
| --- | --- | --- | --- | --- |
| `.apkg` import | 0.26 MiB | 29.34 MiB | 5,000 | 1.64 s |
| `.apkg` import | 5.96 MiB | 9.80 MiB | 5,000 | 2.02 s |
| `.prepdeck` import | 1.72 MiB | 1.72 MiB | 5,000 | 0.60 s |
| `.csv` import | 1.53 MiB | - | 5,000 | 1.30 s |
| `.csv` import | 2.06 MiB | - | 5,000 | over the limit |
| `.csv` export | - | 1.56 MiB out | 5,000 | 0.11 s |
| `.prepdeck` export | - | 1.72 MiB out | 5,000 | 0.15 s |
| `.apkg` export | - | 0.31 MiB out | 5,000 | 0.36 s |

So the body cap is per format rather than one number. The CSV importer is
the small one because `splitPreamble` transcribes Python's
`"\n".join(csv_text.splitlines()[i:])`, which materialises the whole
document as an array of lines and then a second full copy: the line-ending
normalisation is load-bearing (a CRLF inside a quoted cell becomes LF, and
the `quoting` corpus profile pins it), so raising this cap means rewriting
that pass to scan and slice rather than split and join. Left alone here:
it is phase-3 code under its own gate.

### A3. Routes and pages

Same URLs, statuses and rendered pages. The three importer pages take
`{ user, outcome, error }` and nothing else. Every branch of
`prep/decks/routes.py:1832-2029` is reproduced: no file part -> 400 with
`Pick a <kind> file to upload.`; `_validate_deck_name` failure -> 400
with the HTTPException detail; `.apkg` `ValueError` -> 400 with the
message; otherwise 200 with `outcome`. CSV decodes UTF-8 with
replacement on bad bytes. `.prepdeck` refuses an existing deck name
(restore, not append).

Exports keep their exact headers: `.prepdeck` is `application/zip`,
`.apkg` is `application/octet-stream`, both with
`Content-Disposition: attachment; filename="<name>.<ext>"` and
`Cache-Control: no-store`; 404 for a deck the user does not own, the
same shape as not-found.

The two MCP tools drop `APKG_PENDING` and call the codecs.
`prep_import_apkg` keeps its existing arg validation and error strings.

### A4. Byte parity, in three honest tiers

**Tier 1, byte-identical: `export.csv`.** CRLF terminator, minimal
quoting, the preamble for trivia decks. Already implemented; gated here.

**Tier 2, byte-identical: `.prepdeck`.** Python's writer is already
deterministic: `ZIP_STORED`, `date_time=(1980, 1, 1, 0, 0, 0)`,
`json.dumps(indent=2, sort_keys=True) + "\n"`. `fflate.zipSync` with
`level: 0`, `mtime` at the same 1980 stamp, `os` matching Python's
`create_system = 3`, versions 20/20, `external_attr = 0`, no data
descriptor and no zip64 under the A2 caps, reproduces it. Entry order:
`meta.json`, `cards.csv`, `reviews.csv`, then `trivia_queue.csv` for a
trivia deck.

**Tier 3, NOT byte-identical: `.apkg`, and Python is not identical to
itself.** Two measured reasons, not hypotheses:

- `csum = abs(hash(front)) % 10**10` uses CPython's randomized string
  hash. The same string in two processes:
  `2392411565`, then `5387854239`.
- `zipfile.write` / `writestr` with no explicit `ZipInfo` stamps the DOS
  timestamp from the wall clock, and `ZIP_DEFLATED` output depends on
  the zlib build.

So the gate is a canonical dump, not bytes: entry names in order;
`media` byte-equal (`{}`); the collection compared as `SELECT *` over
`col`, `notes`, `cards`, `revlog`, `graves` in id order, `csum` the one
excluded column with this paragraph as the reason; the `models` /
`decks` / `conf` / `dconf` / `tags` blobs parsed and compared
structurally. The TS writer is deterministic where Python is not:
`csum` takes Anki's real definition (first 8 hex chars of SHA-1 over the
sort field, as an integer) and the zip takes the 1980 stamp. Both are
non-visual and non-contractual.

### A5. Corpora

`tests/parity/oracles/deckio.py` extracts from the Python app into
`tests/parity/goldens/deckio/`, read-only afterward. Nine profiles:
`srs-mixed` (all four question types), `trivia` (preamble, all three
keys), `empty`, `unicode` (CJK, emoji, RTL), `quoting` (embedded quotes,
commas, CRLF and LF inside cells), `fsrs` (card state plus a reviews
log), `trivia-queue`, `anki-legacy` (`collection.anki2`), `anki-cloze`
(cloze, single-field and HTML-entity notes).

Export direction, per profile: `<p>.csv`, `<p>.prepdeck`, `<p>.apkg`,
`<p>.apkg.dump.json`. Import direction: the corpus bytes plus
`<p>.import.json`, the rows Python produced from them (questions in id
order with every column, card state, reviews, trivia queue). The three
`.apkg` sources are generated, not downloaded: no third-party deck is
committed.

`worker/tests/deckio.parity.test.ts` runs both directions against the
corpora through `tests/pyoracle.ts`.

## B. `/metrics` and the last route

`/metrics` stays on the entry worker, unauthenticated, same path,
`Cache-Control: no-store`. `app/metrics.ts` is a small histogram
registry with the OpenMetrics text encoder; `runtime/worker.ts` observes
every request the way `http_metrics_middleware` does, skipping
`/metrics` itself and labelling `route` with the matched pattern or
`<unmatched>`.

**Kept, same names, labels and bucket boundaries** so an existing query
still resolves: `prep_http_request_duration_seconds` (`method`, `route`,
`status`), `prep_ai_grade_duration_seconds` (`verdict`),
`prep_instant_generate_duration_seconds` (`outcome`).

Only the first of the three has a caller. `prep_ai_grade_*` and
`prep_instant_generate_*` are declared and exposed, so a query against
them resolves to a family rather than to nothing, but a deployed
`/metrics` prints their `# HELP` and `# TYPE` and no samples. Both labels
are finer than the value their use case returns, so observing them means
a port the composition root wraps rather than a call inside the use case,
and that wrapper is not part of this phase. Nothing may alert on either
until it has a sample.

**Gone, and this is the documented reduction:**

- `prep_anyio_threadpool_borrowed`, `prep_anyio_threadpool_capacity`.
  There is no threadpool; the signal they carried does not exist.
- Everything `prometheus_client`'s default registry shipped for free:
  `python_info`, `python_gc_objects_collected_total`,
  `python_gc_objects_uncollectable_total`, `python_gc_collections_total`,
  `process_cpu_seconds_total`, `process_resident_memory_bytes`,
  `process_virtual_memory_bytes`, `process_start_time_seconds`,
  `process_open_fds`, `process_max_fds`. A cell has no process to
  report on.
- `prep_*_duration_seconds_created`, one per histogram child.
  `prometheus_client` emits a `_created` gauge beside every histogram
  unless `disable_created_metrics()` is called, and the reference does
  not call it. The byte gate narrows the oracle with that call rather
  than reproducing a timestamp series with no reader.

**What it can and cannot say.** Module-level state is per isolate and
shared by every cell of the worker on that node (spike 5, 5.1). The
counters are one isolate's, they reset when it is recycled, and each
node has its own. A scrape through the ingress lands on an arbitrary
isolate, so as a Prometheus target the ingress produces jumping values
and phantom counter resets: worse than no target. The honest reading is
per-isolate sampling; a scrape config must target node addresses
directly with a per-node `instance` label.

Lane B also closes the inventory: `routeTable.test.ts` asserts
`OUT_OF_SCOPE` holds exactly the routes the Python inventory itself
serves under `/_debug/` or `/debug/`, so a debug route added to the
reference fails the gate rather than joining a hand-written list.

**Operator note**, in `infra/prep/DEPLOY-CELLD.md` under a new
`## Observability, after the rewrite` heading: the three surviving
metric families and their labels, and which of them has a caller; the
series that are gone and why; the per-isolate semantics; the rule that a
scrape targets nodes, never the ingress. Nothing scrapes prep today (no
prep target exists under `infra/observability/prometheus/`), so this is a
requirement for whenever one is wired, not a migration. Commit in
`infra`, no push.

## C. The e2e suite

`tests/e2e/` is 23 files: 20 test modules, `conftest.py`,
`flow_artifacts.py`, `__init__.py`.

### C1. Classification

**Carry over untouched, against the celld fleet (4 modules):**
`test_smoke.py`, `test_ai_flows.py`, `test_browser_smoke.py`,
`test_free_inference_smoke.py`. They take `http` / `page` off
`deployed_target`, authenticate with `E2E_API_TOKEN` and a Clerk storage
state, and assert through public URLs. Two docstring edits only:
`test_ai_flows.py` and `test_local_browser_smoke.py` name Temporal in
prose.

**Rewritten against a local celld node (16 modules):** the 13 taking
`offline_server` (`test_offline_{study,author,m5,parity,wipe}_e2e.py`,
`test_online_{study,host}_e2e.py`, `test_merge_offline_e2e.py`,
`test_dashboard_{components,parity}_e2e.py`,
`test_study_components_e2e.py`, `test_markdown_parity.py`,
`test_local_browser_smoke.py`) plus the three building their own variant
on `LocalOfflineServer`: `test_device_wipe_e2e.py` (`wipe_server`),
`test_landing_local_decks_e2e.py` (`landing_server`),
`test_instant_start_e2e.py` (`instant_server`). The rewrite is the
fixture and the seed, not the assertions.

**Retired: no whole module.** Inspected: nothing asserts the Go worker
or Temporal as a subject. Two smaller retirements:
`test_no_blocking_handle_result_in_polling_routes` keeps its assertion
(a polling route answers promptly) and loses its rationale, a Temporal
long-poll; and `E2E_TAILSCALE_LOGIN`, the header spoof in the `http`
fixture, goes, replaced by the fake identity provider plus
`X-Internal-Token` (decision 7.0).

### C2. The local node fixture

`tests/e2e/celld_node.py`: `LocalCelldNode`, the same surface
`LocalOfflineServer` has, so a suite changes one fixture line.

```
class LocalCelldNode:
    def __init__(self, name, *, vars: dict[str, str] | None = None)
    base_url: str
    seed: dict
    def start(self, timeout: float = 90.0) -> None
    def stop(self) -> None
```

- Wraps `worker/scripts/run-node.sh`, which already has `stop`,
  `SKIP_BUILD`, `SKIP_DEPLOY`, `PREP_DEV_PORT`, `PREP_DEV_STATE_DIR`
  and `PREP_DEV_S3_BUCKET`. A session-scoped `celld_build` builds once;
  each node deploys that build to its own bucket prefix on the scratch
  MinIO, own free port, own state dir.
- Per-suite env becomes `CELLD_VAR_*`, sealed at deploy, so a differing
  shape (`landing_server`'s Clerk mode, `wipe_server`'s fake provider,
  `instant_server`'s opened-up limiter) is a separate deploy of the
  same build.
- `stop()` runs `run-node.sh stop`. The port then refuses, which is what
  the offline suites need: `ctx.set_offline` does not reach a service
  worker, so killing the server is still the only real offline
  simulation.
- `start()` after a `stop()` uses `SKIP_BUILD=1 SKIP_DEPLOY=1`, and it
  is NOT enough to wait for `/healthz`. Cells are unreachable for 6-8 s
  after a node restart while the lease TTL expires (spike 6, 5.1), so
  `start()` polls a real cell read, `GET /api/dashboard/overview` under
  an anonymous cookie and no identity headers. Identity headers are
  wrong here: the two suites that most need the wait (`landing_server`,
  `instant_server`) deploy the clerk shape, where the router refuses an
  unverifiable identity with a 401 before any cell is touched, so
  accepting that answer degrades the wait to `/healthz` on exactly those
  nodes. `prep_anon` reaches a cell under every provider.

  Two answers count, and both are the cell's. A 200 is one. The other is
  the probe's own account: `prep_anon` names an anonymous row that only
  the instant mint creates, so on a fresh node the cell answers 410
  tombstoned and the router turns that into a 401 that clears the
  cookie. Nothing but a cell produces that clear on a cookie signed with
  the node's own key, and holding out for a 200 a fresh node can never
  give is a fixture that always times out. A 5xx, and a bare 401 or 303
  with no clear, both mean not ready. `router.test.ts` pins the two
  answers apart. Budget 90 s: the deploy, the lease expiry, and a cold
  isolate on a loaded box. Every `server.start()  # idempotent` call site
  in the instant suite keeps working unchanged.
- Because a node is heavier than a uvicorn and the box is
  memory-constrained, e2e runs ONE file per invocation. Fixtures are
  lazy, so at most one node is live per run.

### C3. Seeding and identity

`_seed_offline_db`, `_seed_fake_mode_db` and the landing suite's direct
sqlite writes are deleted: there is no file to open. They become seed
profiles under `worker/runtime/cells/seed/`, reached through
`POST /_parity/seed`, returning the same id dicts the fixtures put on
`.seed`:

- `offline_e2e`: the deck `offline-e2e` with the mcq, the regex short,
  the plain short and the suspended card, each due in its own past hour
  so the oldest-first order is pinned. Returns
  `{deck_id, mcq_id, regex_id, short_id, suspended_id}`.
- `device_wipe`: the fake-provider deck and its `qids`.
- `landing` and `instant`: empty, the visitor shapes.

Header injection in every local context gains `x-internal-token`
alongside `tailscale-user-login` and `tailscale-user-name`: the fake
provider verifies nothing else, so the token is what stops any caller
reaching any user's cell. The two empirically-pinned Playwright facts in
the `conftest.py` comment block still hold; the comment stays, with
uvicorn renamed.

### C4. The refused write

Phase 4 recorded that a single local node runs its log ensemble degraded
and occasionally answers 500 `DurabilityUnproven` on a write, and left
open whether the ingress may retry. Retrying inside the fixture is
forbidden: it would hide a defect a user meets as an error page.

**Measured: 20 consecutive runs of `test_offline_study_e2e.py` against a
local node, 60 tests, 0 failures and 0 refusals.** The degradation is
real and the node names it (`log ensemble degraded; acks ride the bucket
... why="member append failed"`, twice per start), but the fallback path
answered every write the suite makes. So neither of the two outcomes the
lane named applies: there is no rate to fix at the ingress and nothing to
escalate. What stays true is the shape of the risk, not a number: one
node cannot form a quorum, so its acks ride the bucket, and a slower
bucket is what would turn this into a refusal. The retry in
`seed_profile` stays, because it covers the one write a suite makes
before it has a page to show an error on.

## D. Gates, with numbers

- **A, byte parity:** 9 profiles x 2 directions, 0 mismatches.
  `.csv` and `.prepdeck` 0 differing bytes; `.apkg` 0 differing rows in
  the canonical dump with `csum` the only excluded column.
- **A, caps:** the four measured peak-heap numbers in A2 recorded, each
  under 64 MiB.
- **B:** the three surviving metric families byte-equal to
  `prometheus_client`'s exposition for the same observation sequence
  (`tests/parity/oracles/metrics.py`); `routeTable.test.ts` green with
  `OUT_OF_SCOPE` down to the two 7.6 routes and all 139 Python routes
  classified exactly once.
- **C:** 20 e2e modules, 0 failures. 4 against the fleet
  (`E2E_BASE_URL=https://celld.staging.prepcards.app`), 16 against a
  local node, one file per invocation.
- **D, pixel:** five flows registered at phase 5 (`import-csv`,
  `import-prepdeck`, `import-anki`, `export`, `split`) on the `io` seed
  profile, both schemes, 28 shots. Each importer flow carries its form
  state, an outcome state and an error state; `export` carries the hub
  for an SRS and a trivia deck; `split` carries the form, the refusal a
  selection-less post earns and a selection. The error state is reached
  with a reserved deck name, not an empty file input: the input carries
  `required`, so no browser posts that case.

  Goldens are captured from the Python app, then compared against the
  celld target. A flow now declares whether it needs the job stack
  (`jobs=True` on the six phase-4 flows), so capturing these five against
  a local Python target no longer demands a Temporal devserver and a
  built Go worker for screens that touch no job.

  `test_registry.py` resolves all five templates. Its
  `test_every_page_template_and_partial_is_covered` stays `xfail`: three
  templates from earlier phases (`partials/notif_edit.html`,
  `partials/pin_form.html`, `settings_account.html`) still carry no flow,
  and the reason string now names them instead of the phase.
- **Integration, once:** `cd worker && npx vitest run && npm run
  typecheck`, then the pixel files one at a time against the fleet.

## E. Out of scope

Migration and cutover, all phase 6: the per-user and global exporters,
the idempotent importer, the rehearsal against a prod snapshot, PAT
reissue, the VAPID key conversion, the runbook, promotion, deleting the
Python tree and the Go worker. Also out: `CelldWorkflowsRunner` (when
0.3.1 has an artifact), the debug endpoints (7.6), any visual change.

## F. Lanes, files, commands

| lane | owns | run only |
| --- | --- | --- |
| A | `app/decks/{anki,ankiExport,archive}.ts`, `app/ports.ts` (the three codec ports), `runtime/adapters/{apkg,zip}.ts`, the import/export handlers in `runtime/cells/routes/pages.ts`, `app/api/mcp.ts` (the two tools), `worker/package.json` (sql.js, fflate), `tests/parity/oracles/deckio.py`, `tests/parity/goldens/deckio/**`, `worker/tests/{anki,archive,deckio.parity,apkgAdapter}.test.ts` | `cd worker && npx vitest run tests/anki.test.ts tests/archive.test.ts tests/apkgAdapter.test.ts tests/deckio.parity.test.ts tests/layering.test.ts && npm run typecheck` |
| B | `app/metrics.ts`, the observe hook in `runtime/worker.ts`, `runtime/routes/metrics.ts`, `tests/routeTable.test.ts`, `tests/metrics.test.ts`, `tests/parity/oracles/metrics.py`, `infra/prep/DEPLOY-CELLD.md` | `cd worker && npx vitest run tests/metrics.test.ts tests/routeTable.test.ts tests/layering.test.ts`; `.venv/bin/pytest tests/parity/test_ts_metrics.py -q` |
| C | `tests/e2e/celld_node.py`, `tests/e2e/conftest.py`, the 16 rewritten modules, `worker/runtime/cells/seed/{offlineE2e,deviceWipe,landing,instant}.ts`, `worker/tests/seed.test.ts` | `cd worker && npx vitest run tests/seed.test.ts`; then `.venv/bin/pytest tests/e2e/<one_file>.py -q`, ONE FILE PER INVOCATION |
| D | `tests/parity/flows/{import_csv,import_prepdeck,import_anki,export,split}.py` and their `test_flows_*.py`, `tests/parity/harness/registry.py` (the phase-5 rows), `prep/dev/parity_seed.py` (an `io` profile) | `PARITY_PHASE=5 .venv/bin/pytest tests/parity/test_flows_<flow>.py -q`, ONE FILE PER INVOCATION |

Order: A and B in parallel; D's Python half (the `io` seed profile and
the goldens from the Python app) runs beside them; D's compare half runs
after A. C runs last, because the local-node fixture needs the phase's
routes present or the smoke modules fail on a 404.
