# Phase 6: migration and cutover

Scope: 5.8. The exporter, the importer, the verifier, the VAPID
conversion, and the gates. The runbook is `infra/prep/CELLD-CUTOVER.md`;
nothing here names a production host, path or credential. Out of scope:
running the real cutover, and deleting the Python tree (that follows a
clean cutover).

The migration tools live in `prep/migrate/` (they read the Python schema
and die with it) and take the snapshot and the fleet as parameters. No
tool holds a default path or a default base URL.

---

## A. The export

### A0. The snapshot, and the invariant

The input is a **snapshot file**: one self-contained SQLite database
produced from the live one by `VACUUM INTO`, which folds the WAL in and
writes nothing to the source. Producing it is the operator's step, in
the runbook; every tool below takes `--snapshot <path>`.

**Invariant: the exporter is a pure read.** It opens the snapshot as
`file:<path>?mode=ro&immutable=1`, sets `PRAGMA query_only = 1`, and
never calls `prep.infrastructure.db.init()` (which would run 27
migrations against it). It writes only under `--out`.

Proved by `tests/migrate/test_export_is_pure.py`: hash the fixture
snapshot with `sha256`, record `st_size` and `st_mtime_ns`, run the full
export, re-hash. All three equal, and no `-wal` or `-shm` sidecar exists
next to the snapshot afterwards (a read-write open creates them even
when no row changes, so their absence is the second, independent
witness).

### A1. The on-disk format

A directory, stable across runs, diffable, streamable:

```
<out>/manifest.json
<out>/directory/{users,account_merges}.ndjson
<out>/limiter/instant_generations.ndjson
<out>/users/<b64u(user_id)>/profile.json
<out>/users/<b64u(user_id)>/<table>.ndjson
```

One JSON object per line, keys in `PRAGMA table_info` order, rows in
`ORDER BY rowid`. TEXT as string, INTEGER and REAL as JSON number, NULL
as null. The exporter asserts no value is `bytes`: this schema has no
BLOB column and a BLOB appearing later must fail loudly rather than be
coerced.

REAL is written with Python's shortest round-tripping repr, which is
what `json.dumps` already emits and what `JSON.parse` reads back
bit-identically. `tests/migrate/test_export_floats.py` pins it: for
every `cards` row, `struct.pack('>d', json.loads(line)["stability"])`
equals the source column's bit pattern.

`manifest.json` carries the snapshot sha256, the tool version, the
Python schema fingerprint (sorted `PRAGMA table_info` of all 18 tables),
`generated_at`, the per-user `idx`, and the per-user per-table row
counts. The counts are the verifier's expectation and the importer's
resume target.

**`idx` is assigned by the exporter**, deterministically: rank in
`ORDER BY created_at, tailscale_login`, starting at **1**. Never 0 (the
parity seed owns block 0). Every imported row keeps its Python id, all
far below `ID_BLOCK` (2^32); seeding the cell's sequences to `idx * 2^32`
afterwards means no row minted after the cutover can collide with a
migrated one in a later merge.

**A rank is only stable while the set of users is**, and it is not: prod
is still serving at the first snapshot, so an account can be deleted, or
reaped by Python's own `IDLE_DAYS = 365` sweep, before the second. That
renumbers every user after it, and nothing on the fleet can follow -
`DirectoryCell.users.idx` is `NOT NULL UNIQUE` and `register` returns the
idx a user already has, so the second pass would report one divergence per
shifted user, a new signup would collide on the vacated number, and no
re-run could clear either. So **the second export carries the first's
assignment forward**: `export --carry-idx-from <manifest.json>`. A user in
it keeps its number, a new one is allocated above every number handed out,
and a deleted user's number is retired rather than reused.
`register` refuses an idx another account holds with a 422 naming both,
not a bare `UNIQUE constraint failed`.

### A2. Per-user tables

Copied verbatim, user columns dropped (the cell has none):

`decks`, `questions`, `cards`, `reviews`, `grading_idempotency`,
`offline_sync_idempotency`, `study_sessions`, `study_session_answers`,
`trivia_sessions`, `trivia_queue`, `notifications_log`,
`push_subscriptions`, `byok_credentials`, `api_tokens`.

Four dispositions that are not a straight copy:

| table | disposition | why |
| --- | --- | --- |
| `active_workflows` | **reset**, not exported | every row names a Temporal execution that stops existing when the Go worker is torn down; a non-terminal row would make the badge poll a `JobCell` with no ledger and render `gone` forever. It is a 60 s read model, so nothing durable is lost |
| `byok_credentials` where `provider = 'claude-subscription'` | exported, **dropped at import**, counted | decision 7.4. The export stays a faithful copy of the snapshot; the policy lives in one place, the importer |
| `api_tokens` | copied verbatim | the hashes are of legacy-format tokens, which `parseToken` rejects, so none can authenticate. Rows are kept so the settings table still shows the token the holder knows about; the reissue notice (E) is the user-facing half |
| `questions_idempotency`, `steps_idempotency`, `job_progress`, `tombstone` | left empty | no Python counterpart |

`study_sessions.current_grading_workflow_id` copies as-is. A session
left in `state = 'grading'` across the window resolves to `gone` and the
study loop recovers, as it does for a terminated execution today. The
exporter reports the count; a large one is a signal to pick a quieter
window, not a blocker.

### A3. Global tables

| Python table | lands in | reasoning |
| --- | --- | --- |
| `users` | **split.** `(id, is_anonymous, created_at, idx)` into `DirectoryCell.users`; the other 8 columns into that user's `profile` row | enumeration in the directory, everything else per-user (2.1) |
| `users.last_seen_at` | the user's `profile`, verbatim | it is the anonymous reaper's only input. Resetting it to `now` would spare every idle anonymous account for a fresh full retention period; resetting it to the epoch would delete them all on the first sweep |
| `account_merges` | `DirectoryCell.account_merges`, ids preserved | it is the source of `previous_ids`. An offline device learns its old owner id from it; losing a row silently orphans that device's queue |
| `instant_generations` | `InstantLimiterCell`, filtered to `created_at >= generated_at - 48h` | it is the limiter's window source. A reset hands every IP a fresh burst allowance at the moment of highest exposure. 48 h is twice the widest window, so a dropped row can never change a decision, and it keeps the largest global table small |
| `merge_markers` | no counterpart; **not created** | a Python merge still `status='started'` at export has no celld marker and will never resume. The exporter counts those rows and the count is an abort criterion (E) |

---

## B. The import

`POST /_migrate/import` on the entry worker, gated on `X-Internal-Token`
exactly as `/_parity/seed` is (503 unconfigured, 401 mismatched). It is
**not** behind `refusePinsOutsideParityHosts`: it has to run where the
data goes. `POST /_migrate/seal` writes a one-way flag into the
`DirectoryCell`; every later `/_migrate/*` call answers 410. The
runbook seals after the cutover verifies.

One chunk, one user, one table:

```json
{"user":"<id>","idx":7,"table":"reviews","rows":[...],"profile":{...}|null}
```

**Idempotency key: the row's own primary key inside the cell.**
`ExportRepo.importRows(snapshot, {idempotentBy:'id', conflict:'update'})`
is one upsert per row under one `transactionSync`:

```sql
INSERT INTO t (...) VALUES (...)
ON CONFLICT(<primary key>) DO UPDATE SET <every other column> = excluded....
WHERE <any of them differs>   -- `IS NOT`, so NULL compares
```

`INSERT OR IGNORE` would be idempotent for a **replay** and inert for a
**changed row**, and the second pass exists for changed rows. `cards` is
the table that makes this load-bearing: it is never inserted into after
creation and always rewritten, so every review moves `stability`,
`difficulty`, `next_due`, `step` and `fsrs_state`. An import that could
only insert would put a studying user's pre-window schedule on the fleet,
and **no re-run of the same import could repair it** - the key is already
there. The same holds for `study_sessions`, `trivia_queue`,
`trivia_sessions.queue/done`, `decks.pinned_at` and its notification
columns, `questions.suspended`, `notifications_log.seen_at`,
`push_subscriptions.last_seen_at`, `byok_credentials.last_used_at`,
`api_tokens.last_used_at`, and `account_merges.status` in the directory.

The `WHERE` guard is what keeps the count meaningful: a row re-sent
unchanged writes nothing, so `inserted` is **rows inserted or changed**
and a replay of an unchanged export still reports zero. The runbook reads
the second pass's total as the window's writes, and that is what it is.

`conflict:'ignore'` stays the merge's mode: two cells mint from disjoint
id blocks, so a collision there is a bug and the target's row is the one
to keep.

`directory.register`, `seedIdBlock` (raises a counter, never lowers it)
and `prefs.upsert` are each idempotent on their own key. Nothing in the
path is keyed by a run id, so two concurrent runs of the same export
converge too.

**The run header.** A run opens with one chunk of its own,
`{"snapshot":"<sha256>"}`, before any user. It does two things. It records
on the `DirectoryCell` which snapshot this fleet is being built from, so
the verifier can refuse to compare a fleet against a snapshot it was not
built from (C). And it **holds the fleet's anonymous-retention sweep off**
until `POST /_migrate/seal`, with a 30-day backstop for a run that was
rolled back and never sealed. Without that hold, `importUserChunk`
registers an account into the directory one RPC before its cell is
written, and the sweep reads a profileless cell's Python `created_at` -
years old - as idle and destroys it. That damage is permanent:
`UserCell.importChunk` refuses every chunk for a tombstoned cell and
nothing un-tombstones one. The reaper carries the same rule
independently: a cell with no profile and no tombstone is skipped, never
judged by the directory's date.

**Order per user**, all idempotent: `register(id, isAnonymous,
created_at, {idx})` → `seedIdBlock(idx)` → profile upsert (with
`active_byok_provider` nulled when it names a dropped provider) →
`setIdBase(idx)` → the tables in `DATA_TABLES` order, parents first.

**Partial failure mid-user.** A chunk is one transaction: it lands whole
or not at all. A user abandoned halfway keeps the chunks that landed and
holds no half-row. The re-run replays from the start of that user and
every landed chunk applies nothing.

**Resume point, server-side, and NOT the default.** `GET
/_migrate/status?user=<id>` returns `{table: COUNT(*)}` for every data
table plus the profile's presence. `--resume` compares it against the
manifest's counts and restarts at the first short table; because the
write is order-independent, re-sending a whole table is always safe.

**But counts cannot say whether a table is up to date**, only whether it
is short, and the second pass's whole job is rows whose count did not
move. So a run sends every chunk by default, `--resume` is the explicit
opt-in for restarting a killed run, and the profile chunk goes every time
even under `--resume`: it is the only carrier of `display_name`, `email`,
`notification_prefs`, `editor_input_mode`, `active_byok_provider`,
`desired_retention` and `last_seen_at`. The globals are sent every run for
the same reason - a count-based skip compares totals, not identities, so a
fleet holding a ledger row of its own would reach the expected count and
skip the real import.

**Bounded against the 128 MB isolate**, the phase 5 A2 way: caps
enforced before any parsing, `Content-Length` first and a chunked body
counted while read.

| limit | value | on breach |
| --- | --- | --- |
| request body | 4 MiB | 413, `{"detail":"chunk over 4 MiB"}` |
| rows per chunk | 2,000 | 413, `{"detail":"chunk over 2000 rows"}` |
| tables per chunk | 1 | 422 |

The exporter emits chunks inside both caps, so a user with 5,000
questions and 50,000 reviews arrives as 28 chunks and the cell holds one
at a time: the body string, the parsed array, and the insert. The
endpoint refuses over the cap so a hand-rolled call cannot OOM the cell
either. Measured under `CELLD_V8_HEAP_LIMIT_MB=64` like phase 5, and the
peak recorded in the gate.

---

## C. Verification

`prep/migrate/verify.py`, standalone against **any** snapshot plus fleet
pair:

```
.venv/bin/python -m prep.migrate.verify \
  --snapshot <path> --base-url <url> --token-file <path> \
  [--users <file>] [--json <report>]
```

It reads the cell side through `GET /_migrate/dump?user=&table=&after=&limit=`
(paged by rowid, same token, response capped at 2,000 rows so the dump
is bounded by the same argument the import is). Exit 0 clean, 1 with a
report.

**Tier 1, counts.** Per user, per table, snapshot count equals cell
count, exactly. Plus the globals: `DirectoryCell.users` count and every
`account_merges` row field by field (`previous_ids` reads them), and the
limiter ledger count against the 48 h filter.

**Tier 2, FSRS state, field by field, as an exact oracle.** Every
`cards` row, joined by `question_id`:

| field | type | compared |
| --- | --- | --- |
| `question_id` | INTEGER | `==` |
| `step` | INTEGER | `==` |
| `fsrs_state` | INTEGER | `==` |
| `next_due` | TEXT | **string equality**, not parsed |
| `last_review` | TEXT / NULL | string equality |
| `stability` | REAL / NULL | **bit-exact**: `struct.pack('>d', v).hex()` |
| `difficulty` | REAL / NULL | bit-exact |

Plus `decks.desired_retention` and `profile.desired_retention`,
bit-exact, because both already shaped the `next_due` values being
copied.

No tolerance anywhere in tier 2. This is a copy, not a computation; the
phase 2 1e-9 tolerance belongs to the FSRS *port* and using it here
would hide exactly the drift being hunted. Timestamps are compared as
strings for the same reason: `2026-08-26T14:00:00+00:00` and
`...T14:00:00Z` are the same instant and a different byte, and the
second one changes what a golden renders.

**Tier 3, the schedule oracle.** Bytes agreeing is necessary, not
sufficient: a card can copy perfectly and still schedule differently if
the retention resolution changes. For every migrated card, at a fixed
clock, run `scheduleReview(state, verdict, now, {desiredRetention:
resolve(deck, user), fuzz: false})` for both verdicts prep exposes
(`right` and `wrong`; the other half of the FSRS scale is not reachable
from the study loop) on both sides - `py-fsrs` against the snapshot,
`domain/fsrs` against the cell
dump - and compare `stability` and `difficulty` at 1e-9, `next_due`,
`fsrs_state`, `interval_seconds` and `step_bucket` exactly. This is the
phase 4.2 corpus comparison re-aimed at real rows, and it is what proves
a silently drifted due date cannot ship.

**Reuse of the phase 2 `domain/py.ts` helpers**, in
`worker/tests/migrate.oracle.test.ts`: `parseIso` + `isoUtc` round-trip
every migrated timestamp column and assert the re-serialised string is
byte-identical to the stored one (the `+00:00` / `Z` class);
`pyFormatG` renders any mismatch the way Python prints it, so a report
line can be pasted into a Python repl. `pyRound` is used only for
reporting.

---

## D. VAPID and push

Python holds a P-256 keypair as PEM (`vapid-private.pem`) plus the
uncompressed public point base64url-unpadded in `vapid-keys.json`. The
worker wants two base64url strings: `PREP_VAPID_PUBLIC_KEY`, the 65-byte
uncompressed point, and `PREP_VAPID_PRIVATE_KEY`, the 32-byte scalar.

`prep/migrate/vapid.py --pem <path> --keys <path>` prints both. It is a
**format conversion of the same keypair**, not a new one.

**Existing `push_subscriptions` therefore survive**, rows copied
verbatim (`endpoint`, `p256dh`, `auth`, `created_at`, `last_seen_at`). A
subscription is bound to the application server key the browser
subscribed with; the push service accepts a VAPID JWT signed by the
matching private key, and the bytes are unchanged. RFC 8291 encryption
uses the subscription's own `p256dh`/`auth`, which the migration does
not touch.

Proved twice. `worker/tests/vapid.migrate.test.ts`: convert a fixture
PEM generated by `py_vapid`, assert the derived public key equals
`public_key_b64()`'s output byte for byte, and that `vapidHeader`
produces a JWT that verifies under that same public key via WebCrypto.
Then on staging, after the import, an actual push to a subscription row
created under the Python app is **delivered and rendered**, not merely
answered 201. A 201 proves the VAPID half only: the push service accepts
a JWT signed by the matching private key without ever looking at
`p256dh`, so a mangled subscription key still returns 201 and the browser
then silently fails to decrypt. `p256dh` and `auth` compared byte for
byte in tier 1 (C) is the other half of that proof.

**If the conversion is skipped** and the fleet mints a fresh keypair,
every migrated subscription breaks: push services answer 403 to a JWT
signed by a key that does not match the `applicationServerKey` the
subscription was created with. The user sees notifications simply stop,
with no error anywhere in the UI, until they toggle push off and on in
`/settings/notifications`. That is the failure mode; the conversion is
mandatory and its verification is a gate, not a nicety.

---

## E. The cutover runbook

The runbook is `infra/prep/CELLD-CUTOVER.md`: it names hosts, namespaces,
secrets and services, so it does not live here. What phase 6 owes it is
the contract below, and the runbook is not done until every line holds.

- **hostthis's shape.** Migrate with prod up, one short announced
  window, the old system never mutated, rollback is an ingress flip that
  **loses the window's writes**.
- **Never mutated** means it: the snapshot is a `VACUUM INTO` read, no
  flag column is written, no row is marked migrated, and the old
  deployment is only ever scaled, never deleted. That is the whole
  reason rollback is one flip.
- **Two passes.** Export, import and verify once with prod still
  serving; then the window opens, and a second snapshot, export and
  import carry only the delta, because every earlier row is already
  keyed (B).
- **The PAT reissue notice is a step the operator runs by hand**, never
  automation. It is one message to one holder (decision 7.3), and a
  script that gets the recipient list wrong mails strangers.
- **Every step carries an abort criterion**, and abort before the flip
  costs nothing because prod is still serving. The full list is in the
  runbook; the four that phase 6 supplies the evidence for are: the
  snapshot sha256 differing before and after the export, a non-zero
  count of `account_merges` rows still `status = 'started'`, a verifier
  that is not clean, and a converted VAPID public key that differs from
  the live app's `/notify/vapid-public-key`.
- **Sealed after the flip.** `POST /_migrate/seal` is the last step, and
  from then on every `/_migrate/*` call answers 410.

## F. Gates

1. **Rehearsal, end to end on staging**, against a synthetic
   prod-shaped snapshot from `prep/migrate/synth.py --users 40 --seed 7`
   (one heavy user at 5,000 questions / 50,000 reviews, ~30 anonymous,
   one mid-merge, one `claude-subscription` row, one PAT holder, push
   subscriptions, `desired_retention` at both clamp ends, cards in every
   `fsrs_state`). Snapshot → export → import → verify, then the whole
   run again on the same fleet: second-run insert counts all zero.
2. **Verification clean**: tiers 1-3, zero mismatches, and the peak
   import heap under 64 MiB recorded.
3. **The pixel sweep still green**, run BEFORE the import or against a
   fleet holding no migrated data. It cannot run after one: every flow
   opens with `POST /_parity/seed`, which wipes the global limiter ledger
   and, through `clearAnonymous`, wipes and deregisters **every**
   anonymous account in the directory - migrated ones included. The sweep
   is a check on the build, not on the migrated rows; the verifier is
   what reads those.
4. **e2e green**: the phase 5 suite, one file per invocation.
5. **VAPID**: the two proofs in D.
