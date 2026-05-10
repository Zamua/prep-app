# Refactor notes — duplication watch (2026-05-03 audit)

A running log of duplication / shared-pattern candidates noticed during
the audit-fix passes. Append-only as we go; pruned + folded into the
real fix in the dedup sweep (audit-fix #5, task #183).

## Format

Each entry: where, what, why-it-matters, suggested-fix. Keep it terse.

---

## Backend

### ✅ TriviaQueueRepo.mark_answered + set_last_correctness — DONE (#183)
- **Where:** `prep/trivia/repo.py` lines ~108 and ~152.
- **Fix shipped:** Extracted `_deck_id_for_question` and
  `_reset_deck_streak` static helpers shared by both methods. Both
  public methods are now ~10 lines each (down from ~25).

### ✅ `_explore_ctx` helper in trivia/routes.py — DONE (#182)
- **Where:** Was called four times (standalone answer, session answer,
  standalone regrade, session regrade) with near-identical args.
- **Fix shipped:** Moved to `prep/trivia/service.py` as
  `build_explore_ctx(*, deck_name, q, user_answer, correct, expected,
  idk=False)`. Routes now `**build_explore_ctx(...)` at all 4 sites.

### ✅ `from prep import db as _legacy_db` in 9 files — DONE (#181)
- **Where:** Was in notify/repo, notify/_legacy_module, study/routes,
  study/repo, auth/identity, auth/repo, trivia/scheduler,
  decks/routes, decks/repo.
- **Fix shipped:** Audit-fix #3 split prep/db.py into per-context
  repos. `prep/db.py` is gone; all 9 files now import from their
  respective context repos. `_legacy_db` is dead.

### ✅ Inline `<script>` blobs across templates — DONE (#6.9)
- **Where:** templates/deck.html (~150 LOC), templates/notify_settings.html
  (~212 LOC), templates/trivia/card.html (~128 LOC).
- **Fix shipped:** Each template's IIFE was hoisted into ES modules
  under `static/js/modules/` (card-preview, improve-dialog,
  qcard-suspend, delete-deck-dialog, notify-settings, trivia-card),
  loaded via the existing importmap + `@/` alias. Per-page boot block
  is now 5-10 lines that imports `init` from each module and threads
  `rootPath` / `vapidKey` / `expectedName` in.

### ✅ Repo direct dict returns (DDD smell) — partially DONE (#183)
- **Where:** `DeckRepo.list_trivia_decks()` was `list[dict]`.
  `DeckRepo.due_breakdown()` is `list[tuple[str, int]]`.
- **Fix shipped:** `list_trivia_decks` now returns `list[Deck]`.
  `Deck` entity gained `notification_ignored_streak` (the only
  scheduler-required field that wasn't already on the entity).
  Scheduler tick + 9 test callsites migrated to attribute access.
- **Still TODO (low priority):** `due_breakdown` returns
  `list[tuple[str, int]]`, which is typed but not entity-shaped. A
  `DueDigestEntry(name, count)` value object would make intent
  clearer; deferred since callers are tiny.

### Test fixtures use `importlib.reload(_legacy_module)`
- **Where:** tests/test_notification_log.py and adjacent.
- **What:** Reloads modules to swap in per-test sqlite. Order-dependent
  + fragile.
- **Suggested fix:** Driven by audit-fix #2 (split _legacy_module). A
  proper `notify_module_for_db(db_path)` factory would let tests build
  a fresh instance instead of mutating module state.

## Frontend

### ✅ Status badge / chip pattern repeats — DONE (#6.6)
- **Where:** `.notif-pill`, `.edit-pill`, `.tag`, `.notif-preset`,
  `.deck-mastery-chip`, `.tag-pin`.
- **Fix shipped:** New `static/css/components/pill.css` holds the
  shared chrome via a multi-selector rule + adds modifiers
  (`.pill--paused`, `.pill--open`, `.pill--danger`, `.pill--mastery`).
  Per-component rules in deck-page.css + card-index.css trimmed to
  size / state delta only (font-size, padding, min-width).

### ✅ Mastery bar duplication — DONE (#6.8)
- **Where:** `.deck-mastery-bar` (lg, deck page) + `.deck-mastery-mini-bar`
  (mini, index card).
- **Fix shipped:** New `static/css/components/mastery-bar.css` with
  a single ruleset + `--track-h` / `--right-fill` / `--wrong-fill` /
  `--track-bg` knobs, plus a `.mastery-bar--mini` modifier.
  `templates/macros/mastery.html` exposes a `mastery.bar()` macro
  both pages call. Old per-variant rules removed.

### ✅ Color-mix(in srgb, var(--X) N%, transparent) repeats — DONE (#6.5)
- **Where:** ~115 raw callsites across static/css/components/.
- **Fix shipped:** Semantic tokens added to tokens.css —
  `--right-bg-soft / --right-bg-strong / --right-border /
  --right-border-strong / --right-overlay / --right-fg-soft`,
  mirrored for `--wrong`, plus `--ink-soft-bg-faint / --ink-soft-bg`,
  `--paper-overlay / --paper-overlay-soft / --quill-overlay`.
  Visually-equivalent percentages folded onto a single token.
  Result: 115 → 57 raw callsites (50% reduction; remainder is
  one-off blends specific to a single component).

### ✅ Outline pill summary chrome (Explain / Explore / Re-grade) — DONE (#183)
- **Where:** `.trivia-disc summary` + `.trivia-regrade-btn` (almost
  identical: 0.65rem padding, 0.85rem font, italic uppercase, 999px
  border-radius, var(--rule) border, color-mix paper background).
- **Fix shipped:** Single multi-selector rule
  (`.trivia-disc > summary, .trivia-regrade-btn`) holds the shared
  chrome + active state. Per-element-specific bits (`width: 100%`,
  `:disabled`, icon size on the button; `list-style: none` and
  marker-hide on the summary) live in tiny follow-up rules.
  Net: -25 lines, single source of truth for the active-state tint.

---

## Audit pass #6 (2026-05-10)

A focused 10-item cleanup pass. All commits on branch
`audit-fix-6-cleanup`, message format `audit-fix-6.N: ...`. Suite
grew 314 → 378 (+64 tests across the per-context test scaffolding).

### Backend

- **6.1: route layering — move raw SQL to repos.** Routes were
  reaching into `prep.infrastructure.db.cursor()` to read deck/review/
  session rows directly. Added `DeckRepo.get_meta()` + `DeckMeta`
  entity (used by deck page + trivia notif-edit fragment),
  `DeckRepo.get_trivia_source_meta()`, `ReviewRepo.get_last_user_answer()`,
  `SessionRepo.mark_completed()`. Migrated `prep/decks/routes.py`,
  `prep/study/routes.py`, `prep/trivia/routes.py:_notif_edit_response`.
- **6.2: split_deck service raw SQL → repo.** `prep/decks/service.py`
  now uses `DeckRepo.get_trivia_source_meta()` instead of cursor().
  Service layer is repo-only — no infrastructure imports left.
- **6.3: pull transform_view rendering out of the route.** New
  `service.build_transform_view_ctx()` returns a typed
  `TransformViewCtx`. Route shrinks from ~130 LOC to ~20. Five
  service-level tests added (deck-scope vs card-scope, modification
  fallthrough, unknown-qid skip, reorganize grouping).
- **6.4 a–e: per-context test files.** Added `tests/auth/`,
  `tests/notify/` (repo + service + routes), `tests/agent/`,
  `tests/web/`, plus `tests/study/test_service.py` and
  `tests/study/test_routes.py`. Mirrors the existing `tests/decks/`
  pattern. Picked up one latent bug (PushSubsRepo.list_for_user
  SELECT misses three required entity fields — flagged separately).

### Frontend

- **6.5: color-mix → semantic tokens.** 115 → 57 raw `color-mix`
  callsites. Tokens added to tokens.css (--right-bg-soft / -strong,
  -border / -strong, -fg-soft, -overlay; mirrored for wrong + paper +
  quill; --ink-soft-bg-faint / -bg). Visually-equivalent percentages
  merged on a single token.
- **6.6: .pill base class + modifiers.** New
  `static/css/components/pill.css` holds shared chrome via
  multi-selector rule covering `.notif-pill`, `.edit-pill`, `.tag`,
  `.notif-preset`, `.deck-mastery-chip`, `.tag-pin`. Modifiers
  `.pill--paused`, `.pill--open`, `.pill--danger`, `.pill--mastery`.
  Per-component rules trimmed to size/state delta.
- **6.7: extract macros/question_form.html.** question_new and
  question_edit templates collapse to ~5-line callsites of
  `qf.form()`. The 60-line shared form lives in one place.
- **6.8: dedup mastery bar (lg + mini).** New mastery-bar.css with
  `--track-h` / `--right-fill` / `--wrong-fill` / `--track-bg`
  custom-prop knobs + `.mastery-bar--mini` modifier. New
  `templates/macros/mastery.html` macro called from both deck page
  + index card.
- **6.9: extract inline `<script>` blobs to static/js/modules/.**
  6 new modules (card-preview, improve-dialog, qcard-suspend,
  delete-deck-dialog, notify-settings, trivia-card). Templates
  shrink to a 5-10 line `<script type="module">` boot block each.
- **6.10: promote .freetext-* to forms.css.** Was in study-card.css
  while used by 8 templates. Lifted to forms.css; new
  `.freetext-hint` style (was used in templates but unstyled).

### Notes / follow-ups (not addressed in this pass)

- `PushSubsRepo.list_for_user()` SELECTs only endpoint/p256dh/auth
  but the entity also requires user_id, created_at, last_seen_at.
  No callsite uses the entity-shaped form today (the push sender
  uses `list_for_user_raw`). Worth a one-line SELECT fix in a
  future pass.
