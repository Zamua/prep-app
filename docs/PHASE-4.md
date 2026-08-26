# Phase 4: durable work

Spec for phase 4 of `docs/CELLD-REWRITE.md` (2.1, 2.4, 5.6, risks 2 and
8). Lane A (the runner) lands first and alone. B (the four workflows),
C (routes and UI) and D (periodic work and the AI adapters) then run in
parallel with no shared files. Lane E (gates) starts immediately beside
A, because its first job is capturing goldens from the **Python** app,
which touches nothing under `worker/`.

Every lane: TDD with vitest, DDD layering (`tests/layering.test.ts`
stays green), ports and adapters, no operator context in this repo,
terse comments, no em dashes, no push. Corpora are read-only; a gap is
closed in the Python extractor and re-extracted.

## 0. Settled points

**The status direction pays for the poll.** A `JobCell` writes to its
owner's `UserCell` and never the reverse. The 2s fragment polls and the
5s badge poll read only `UserCell` rows, so a 300s LLM step blocks its
own `JobCell` and nothing else. The one `UserCell -> JobCell` hop is the
signal (accept / reject / feedback / apply), which happens on a click.

**Progress travels with the status.** Python queried Temporal for the
whole progress dict; the partials need the plan items, the diff, the
counts. The status write therefore carries the rendered progress
payload, into a new `UserCell` table:

```sql
job_progress(workflow_id PK, payload JSON, transition INTEGER, updated_at TEXT)
```

`WorkflowRunner.status(id)` reads that row. A missing row renders
`gone`, which is exactly what Python renders when the query handler is
gone.

**The two internal endpoints are deleted, not ported.**
`POST /api/agent/run` and `POST /api/internal/record-review` exist only
so an out-of-process Go worker could call back into the app: `grep`
finds no browser, JS, MCP or API-v1 caller. On celld the step handler
holds the `AgentPort` and writes through the owner's repositories in the
same isolate, so both endpoints have no caller left.
`worker/tests/routeTable.test.ts` moves them from its phase-4 map to a
`REMOVED` map carrying that reason, so the inventory test keeps failing
on an unaccounted route.

**There is no reconciler.** Each `JobCell` drives its own transitions,
so the cross-user 30s walk of `prep/workflows/scheduler.py` has no work.
Its two remaining duties survive: `cleanup_stale_terminal` on badge read
(phase 3 already), and the 24h prune, which becomes one task of the
per-user alarm (D2).

## A. The runner (lane A, lands first)

### A1. The port

`app/ports.ts`, replacing the phase-3 stub:

```ts
export type JobKind = 'PlanGenerate' | 'Transform' | 'TriviaGenerate' | 'GradeAnswer';
export interface WorkflowRunner {
  start(kind: JobKind, input: JobInput): Promise<{ workflowId: string }>;
  signal(id: string, event: { name: string; payload?: unknown }): Promise<JobStatus | null>;
  status(id: string): Promise<JobStatus | null>;
  terminate(id: string, reason: string): Promise<void>;
}
```

`JobStatus = { status: string; progress: Record<string, unknown> }`.
`signal` returns the post-signal status so a route can render the
transient fragment without a second read. `status` reads `job_progress`
in the calling `UserCell`; only `start`, `signal` and `terminate` touch
a `JobCell`. `RunnerUnavailable` stays for a deploy with jobs off.

Workflow ids keep Python's shapes verbatim, since the routes parse them
for ownership: `grade-<deck>-q<qid>-<hex10>`,
`transform-<scope>-<targetId>-<hex10>`, `plan-<deck>-<hex10>`,
`trivia-<deck>-<hex10>`. Hex from the `Random` port, so parity seeds
reproduce.

### A2. The step ledger (`runtime/cells/JobCell.ts`)

```sql
job(id PK, kind, owner, input JSON, state, cursor, created_at,
    deadline_at, deadline_kind, terminal_at, error, transition INTEGER)
steps(step_key PK, name, idx, status, attempt, refusals, next_attempt_at,
      output JSON, error, started_at, finished_at)
events(seq INTEGER PK AUTOINCREMENT, name, payload JSON, at, consumed_at)
outbox(transition INTEGER PK, status, payload JSON, at, delivered_at)
schema_version(one row)
```

`step_key` is the idempotency key and is the Go worker's string where
one exists: `<jobId>-expand-<i>`, `<jobId>-insert-<i>`; the grading
record key is the job id itself; new keys follow `<jobId>-<name>-<i>`.
The write handlers pass it through to the owner's
`questions_idempotency` and `grading_idempotency` ledgers, so a step row
and a data row can never disagree.

`state` is `running | gated | terminal`. `cursor` is the step index the
next activation resumes at. Terminal is a written state, not an
inference: `done`, `rejected`, `failed`.

`migrate(sql)` under `blockConcurrencyWhile` in the constructor, same
shape as the `UserCell`'s.

### A3. `AlarmLedgerRunner` (`runtime/adapters/alarmLedgerRunner.ts`)

One `drive()` loop, called from `fetch` and from `alarm`:

1. Load `job`, the step rows, and the unconsumed events.
2. `domain/jobs/schedule.ts: nextAction(state, now)` (pure) answers
   `run(stepKey) | wait(untilIso) | gate | finish`.
3. Run at most one step per RPC, write its row, append the outbox row,
   flush the outbox, then `ensureAlarm()`.

**Retries are rows.** A failed step writes `attempt + 1` and
`next_attempt_at = now + backoff(attempt)`; the alarm brings the cell
back. Policies transcribed from the Go `RetryPolicy` values:

| step class | attempts | initial | coefficient | cap |
| --- | --- | --- | --- | --- |
| LLM (plan, expand, compute, generate, grade) | 1 | 2s | 2.0 | 30s |
| insert / apply | 3 | 1s | 2.0 | 30s |
| trivia insert | 3 | 500ms | 2.0 | 30s |
| record review | 5 | 1s | 2.0 | 30s |

**`DurabilityUnproven` and the post-restart unreachable window are
refusals, not failures.** They increment `refusals`, never `attempt`,
and back off 250ms doubling to 8s with a 12-refusal cap. The window is
6-8s, so 12 covers it with headroom. A refusal rolls back its whole RPC,
so the retry re-reads and re-decides; no step is ever half-written. This
is also why a step never combines a large write with `deleteAll`.

**The alarm is derived, never held.** `ensureAlarm()` computes
`min(earliest steps.next_attempt_at, job.deadline_at, earliest undelivered
outbox retry)` from the rows alone and calls `setAlarm`. It runs at the
end of every RPC **and** in the constructor, so an eviction, a node
restart or a duplicate fire all converge. A fired alarm with nothing due
is a no-op.

**The human gate is a persisted deadline.** Entering a gate writes
`deadline_at = now + graph.gate.deadlineMs` and `state = 'gated'`. It is
written once: a feedback event re-runs the plan step and does **not**
refresh `deadline_at` (the Go single-timer rule). The alarm firing on
`deadline_at` is a reject.

### A4. The status write

The outbox row is the transition. Flushing calls
`UserCells.jobStatus(owner, { jobId, transition, status, progress, urlPath, kind, deckId, deckName })`,
which in the `UserCell` is one transaction: upsert `active_workflows`,
upsert `job_progress`, and apply `prep/workflows/service.py:
update_status` verbatim (push on the first awaiting-action transition,
push on the first terminal transition **only** when no action push
fired, `set_terminal_at` write-once, `notified_*_at` write-once).
Idempotent by `(jobId, transition)`: a re-delivered transition whose
number is `<= job_progress.transition` is dropped before any side
effect. `delivered_at` closes the loop; an undelivered row is retried on
the same backoff as a step.

### A5. The graphs, as data

`app/jobs/graph.ts` exports `JOB_GRAPHS: Record<JobKind, StepGraph>`.

```ts
type StepNode = {
  name: string;
  kind: 'llm' | 'write' | 'gate';
  fanout?: { mode: 'batch' | 'per-item'; size?: number; from: string };
  retry: RetryPolicy;
  onError?: 'fail' | 'skip';           // skip = the Go "don't fail siblings" rule
  gate?: { events: string[]; deadlineMs: number; refreshOnEvent: false;
           onEvent: Record<string, string>; onDeadline: string };
  status: string;                       // the literal the partial renders
};
```

Handlers register by name in `app/jobs/registry.ts:
StepRegistry.register(name, handler)`. The runner imports the registry,
never a handler; B and C add steps without opening a lane-A file. A
`graph.test.ts` asserts every node name has exactly one registered
handler and that every `status` literal appears in the matching partial.

## B. The four workflows (lane B)

`app/jobs/{plan,transform,trivia,grade}.ts`. Each handler is
`(ctx: StepContext) => Promise<StepOutput>`; `ctx` carries the input,
the prior steps' outputs, the `AgentPort`, the owner's repos over RPC,
`Clock`, and the step key. Prompts and parsers are transcribed from
`worker-go/activities/*` unchanged, since the LLM stub keys on the
message bytes.

**PlanGenerate.** `plan` (llm, status `planning`) -> `gate` (events
`accept | reject | feedback`, 24h, status `awaiting_feedback`) ->
`expand` (llm, `fanout: {mode:'batch', size:4}`, `onError:'skip'`,
status `generating`) -> `insert` (write, per item, key
`<jobId>-insert-<i>`, status `applying`). `feedback` re-runs `plan` with
`PriorPlan` and the feedback text, bumps `progress.round`, returns to
the gate without touching `deadline_at`; a failed re-plan keeps the
prior plan, sets `progress.error = "replan failed: ..."` and stays
`awaiting_feedback`. `accept` writes the transient `accepting` before
returning from the signal RPC; `reject` writes `rejecting` then
`rejected`. Zero successful expansions is `failed` with
`"every card expansion failed"`. The batch barrier is the Go one: batch
`n + 1` starts only when every member of batch `n` has landed.

**Transform.** `compute` (llm, status `computing`) -> for scope `card`,
straight to `apply`; for `deck` and `reorganize`, `gate` (events
`apply | reject`, 1h, status `awaiting_apply`) -> `apply` (write, status
`applying`). `apply` is `worker-go/activities/transform.go:
ApplyTransform` ported into ONE `transactionSync` in the owner's cell:
new decks first (name-collision skip), then modifications, additions,
deletions, card moves with the trivia-queue add/remove rule, then deck
deletions. Partial application is not a state the ledger can reach.

**TriviaGenerate.** `generate` (llm, status `generating`, batch size 25
or the caller's) -> `insert` (write, per pair, status `applying`) with
the `LOWER(TRIM(prompt))` per-deck dedupe, `queue_position = max + 1`,
counting `inserted`, `skipped_dups`, `skipped_invalid`. Empty output is
`failed` with `"the AI returned 0 cards"`.

**GradeAnswer.** `grade` (llm, status `grading`) -> `record` (write,
status `recording`, key = the job id) through `grading_idempotency` and
the FSRS path of `ReviewRepo.record`. Terminal payload is the
`{verdict, state, user_answer, idk}` shape `/api/study/grading/{wid}`
returns.

Progress payloads keep Python's key names exactly (`status`, `plan`,
`round`, `total`, `generated_count`, `inserted`, `skipped_dups`,
`skipped_invalid`, `scope`, `error`, `started_at`, `finished_at`,
`result`), because the partials read them by name.

## C. The routes and UI (lane C)

`runtime/cells/routes/jobs.ts`, all 18 inside the `UserCell`:

- `GET /plan/{wid}`, `/plan/{wid}/status`, `/plan/{wid}/fragment`,
  `POST /plan/{wid}/{feedback,accept,reject}`.
- `GET /transform/{wid}`, `/status`, `/fragment`,
  `POST /transform/{wid}/{apply,reject}`.
- `GET /reorganize`, `POST /reorganize`, `POST /deck/{name}/transform`,
  `POST /question/{qid}/improve` (phase 3 wired it to the stub; it now
  starts a `card`-scope Transform).
- `POST /trivia/decks/{deck_id}/generate`, `GET /trivia/gen/{wid}`,
  `/fragment`, `/status`.

Ownership is parsed from the wid exactly as Python does
(`_require_owns_transform`, `_require_owns_plan`, `parse_grading_wid`);
a guessed wid must 404, not leak. A start calls
`require_funded_workflow` first (`AgentUnavailable` when the tier is
`none`), then `runner.start`, then registers the badge row; a start
failure answers Python's 500 with its message and keeps the deck.

**The polling contract is byte-identical in shape: a non-terminal
fragment carries `hx-trigger="every 2s"` (1.5s for trivia), a terminal
one carries no trigger at all.** The polling-state sets stay where
Python has them (`plan`: everything except `awaiting_feedback, done,
rejected, failed, gone`; `transform`: `computing, applying, rejecting,
''`; `trivia`: everything except `done, failed`). A signal route renders
the fragment from the status the signal RPC returned, so the transient
`accepting` / `rejecting` / `applying` renders without Python's 1ms
yield.

The badge (phase 3) needs no change: the rows it reads now come from A4.

**AI grading in `/api/study/*`.** The phase-3 `RunnerUnavailable ->
selfGrade` branch stays for an unfunded deploy; a funded one now starts
`GradeAnswer` and returns `{pending: {poll, workflow_id, status,
error?}}`. `GET /api/study/grading/{wid}` reads `job_progress`: non
terminal returns the pending shape with `progress.error` attached when
present, terminal-without-result returns `{failed: {code:
"grading_failed", ...}}` after `grading_abandoned`, terminal-with-result
calls `grading_landed` and returns `_verdict_outcome`. Python's 0.5s
bounded wait disappears: the row is written before the transition, so
the result is there or the job is genuinely not done.

## D. Periodic work and the AI adapters (lane D)

### D1. `AgentPort` adapters

`runtime/adapters/agents/{freeTier,anthropic,openai,openrouter,select}.ts`.
Taxonomy: `AgentUnavailable`, `AgentBusy`, `AgentTimeout extends
AgentBusy`, `AgentBudgetExhausted extends AgentUnavailable`.

- Selection mirrors `agent_for_user`: BYOK row (decrypted through
  `Cipher`) beats the free tier; anonymous gets neither. The
  `claude-subscription` branch is gone (decision 7.4).
  `fundingTierForUser` answers `byok | free | none` from row existence,
  failing closed to `byok`.
- Shared (free tier) maps 429 / quota-coded bodies to `AgentBusy` with
  Python's "add your own key" wording; a user's own key maps the same
  signals to `AgentBudgetExhausted`. 401/403 on the shared key logs at
  ERROR and refuses.
- Free-tier calls pass `FREE_TIER_MAX_CARDS_PER_CALL = 5` as the job's
  max-cards / batch-size; BYOK is uncapped. Output cap 32768 for jobs,
  1024 for instant.
- The `JobCell` reads `UserCells.agentConfig(owner) -> { provider,
  ciphertext } | { freeTier: true } | null` once **per LLM step**, never
  cached across activations, so a revoked key stops the next step. The
  plaintext key never crosses an RPC boundary.
- **Timeouts are an operator-visible reduction.** The Go worker allowed
  30m per activity. A celld fetch is bounded by
  `CELLD_FETCH_TIMEOUT_S`. Each LLM step uses
  `AbortSignal.timeout(min(PREP_JOB_LLM_TIMEOUT_S, CELLD_FETCH_TIMEOUT_S - 5) * 1000)`,
  default 300s, and the staging and prod manifests set
  `CELLD_FETCH_TIMEOUT_S=330`. A timeout is a step failure, surfaced to
  the user, not an extension.

Instant generation is unchanged from phase 3 (its three-cell order and
its 60s + 15s budget already ship); phase 4 only swaps the agent it
selects.

### D2. Per-user alarms (`UserCell.alarm`)

One alarm per cell, one `nextWake` computed by
`domain/notify/wake.ts` (pure) from persisted state alone: the
notification prefs, `last_digest_date`, `last_when_ready_at`, the tz,
quiet hours, each trivia deck's `last_notified_at`,
`notification_interval_minutes`, `notification_ignored_streak`,
`notifications_muted_until`, and the earliest terminal `active_workflows`
row past 24h. `ensureAlarm()` runs at the end of every prefs, deck or
job-status write and in the constructor. Tasks, each idempotent:

- **digest**: at the local `digest_hour`, once per local date, guarded
  by `last_digest_date`. Quiet hours do not apply (the chosen hour is
  the schedule).
- **when-ready**: `due_total >= threshold`, 4h debounce on
  `last_when_ready_at`, skipped inside quiet hours.
- **trivia refill**: when `count_unanswered(deck) < session_size`,
  **dispatch a `TriviaGenerate` job** and return. The alarm never calls
  the LLM. Refill runs during quiet hours; the notification does not.
- **trivia notify**: the effective interval is `base * 2 **
  min(streak, 5)`; resume-vs-fresh pick, the mid-session suppression,
  the engagement streak update and `record_notification_fire` are the
  Python tick transcribed.
- **prune**: delete `active_workflows` and `job_progress` rows terminal
  past 24h.

A duplicate fire is a no-op because every task's guard is the persisted
stamp it writes.

### D3. The reaper (`DirectoryCell.alarm`)

Daily. `listAnonymous(after, 50)`, and for each candidate one RPC asking
its own cell for `last_seen_at`; past `IDLE_DAYS = 365` it calls
`destroy('reaped')` (the phase-3 three-step deletion) and
`Directory.remove` plus a directory tombstone. The cutoff string keeps
the `+00:00` suffix. One candidate's failure does not abort the batch;
the next day re-selects. The alarm is re-armed from `last_reap_at` in
the constructor.

## E. Gates (lane E)

1. **Unit, per lane** (section F), `npm run typecheck`,
   `tests/layering.test.ts` green, `tests/routeTable.test.ts` phase-4
   map empty and the two internal endpoints in `REMOVED`.
2. **The replay matrix** (`worker/tests/jobs/replay.test.ts`, fakes,
   fast): for each of **18 kill points** (PlanGenerate 5, Transform 4,
   TriviaGenerate 3, GradeAnswer 3, gate-entry, signal-persisted-not-run,
   deadline-fire), drop the runner between the step write and the next
   RPC, re-activate from the rows alone, and assert **exactly once**:
   the LLM stub saw the step's key once (`GET /_control/requests`), the
   owner's row count is unchanged, and `job_progress.transition` is
   monotonic with no repeated transition number.
3. **The crash matrix** (`worker/tests/crash/restart.test.ts`, a real
   local node, boardtogether's shape): **6 kill points**, one per job
   kind plus both human gates, each killing the node process and
   asserting the same three properties after restart, plus that the
   alarm re-arms without a request. Slow, so it runs once per lane-E
   pass, not per lane.
4. **Pixel.** The `workflows` and `caps` seed profiles get implemented
   in `prep/dev/parity_seed.py`, and six flow modules join the registry:
   `plan`, `transform`, `reorganize`, `trivia-generating`, `grading`,
   `badge`. Shots: plan 10 (planning held, awaiting round 1, replanning,
   awaiting round 2, accepting, generating, applying, done, rejected,
   gone), transform 12 (computing held, the nine diff-card states across
   three deck-scope shots, applying, done, rejected, card-scope
   auto-apply, improve dialog), reorganize 4, trivia-generating 4,
   grading 3, badge 2. **35 shots, 70 goldens.** The stub's `hold()`
   mode is what makes every in-progress screen deterministic: hold,
   capture, release. Goldens are captured from the **Python** app first
   (`make dev`: temporal + app + worker, `PREP_FREE_INFERENCE_BASE_URL`
   at the stub), then compared against the TS app locally and on the
   fleet. `PARITY_PHASE=4`, one file per invocation.
5. **Contracts.** A new extractor `tests/parity/oracles/jobs.py` ->
   `tests/fixtures/parity/jobs/`, recorded against the `workflows`
   profile: every one of the 18 routes, at least one pair per status
   branch of the three progress partials, each fragment pair asserting
   the presence or absence of `hx-trigger`, plus the `/api/study/grading/{wid}`
   pending / failed / landed triple. `tests/parity/test_ts_jobs.py`
   replays it with `dom_diff`. **Acceptance: 100% of the corpus, no
   exception set** (contrast phase 3's 128 of 130). A coverage test
   asserts every `_status` literal of the three partials appears in a
   pair; the extractor's own `test_oracles.py` entry keeps it
   reproducible.
6. **The phase-3 pixel and contract gates stay green**, with their two
   named divergences unchanged.

## F. Lanes, files, commands

| lane | owns | run only |
| --- | --- | --- |
| A | `app/ports.ts` (runner, JobKind), `app/jobs/{graph,registry,status}.ts`, `domain/jobs/**`, `runtime/cells/JobCell.ts`, `runtime/adapters/alarmLedgerRunner.ts`, the `job_progress` migration and `UserCells.jobStatus`, `tests/jobs/{ledger,schedule,graph,statusWrite,replay}.test.ts`, `tests/fakes/alarms.ts` | `cd worker && npx vitest run tests/jobs tests/layering.test.ts && npm run typecheck` |
| B | `app/jobs/{plan,transform,trivia,grade}.ts`, `domain/jobs/progress.ts`, `tests/jobs/workflows/**` | `cd worker && npx vitest run tests/jobs/workflows tests/layering.test.ts` |
| C | `app/decks/{plan,transform}.ts`, `app/trivia/generate.ts`, `app/study/grading.ts`, `runtime/cells/routes/jobs.ts`, `tests/pages/jobs*.test.ts`, `tests/api/grading.test.ts`, `tests/routeTable.test.ts` | `cd worker && npx vitest run tests/pages/jobs.test.ts tests/api/grading.test.ts tests/routeTable.test.ts tests/layering.test.ts` |
| D | `runtime/adapters/agents/**`, `domain/notify/wake.ts`, `UserCell.alarm`, `DirectoryCell.alarm`, `tests/{agents,alarms,reaperAlarm}.test.ts`, the `wrangler.*.jsonc` / staging manifest vars | `cd worker && npx vitest run tests/agents.test.ts tests/alarms.test.ts tests/reaperAlarm.test.ts tests/layering.test.ts` |
| E | `prep/dev/parity_seed.py` (`workflows`, `caps`), `tests/parity/flows/{plan,transform,reorganize,trivia_generating,grading,badge}.py` and their `test_flows_*.py`, `tests/parity/oracles/jobs.py`, `tests/parity/test_ts_jobs.py`, `worker/tests/crash/**` | `.venv/bin/pytest tests/parity/test_seed.py -q`; `PARITY_PHASE=4 .venv/bin/pytest tests/parity/test_flows_<flow>.py -q`, ONE FILE PER INVOCATION; `.venv/bin/pytest tests/parity/test_ts_jobs.py -q`; `cd worker && npx vitest run tests/crash` |

Order: A, then B, C, D in parallel. E's Python half (seed profiles,
goldens from the Python app) runs beside A; its TS half runs after C.
Integration, once: `cd worker && npx vitest run && npm run typecheck`,
the pytest gates above, the pixel files against the local node, then
merge, tag, deploy, and the pixel files against the fleet.

## G. Out of scope

CSV / `.prepdeck` / Anki import and export, `/metrics` (phase 5);
migration and the block-0 importer (phase 6); `CelldWorkflowsRunner`
(when 0.3.1 has an artifact, behind the same port); the debug endpoints
(7.6); any visual change.
