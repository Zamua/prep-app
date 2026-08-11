# AI providers

How prep talks to LLMs today, and the plan for making the layer
above the adapters genuinely provider-neutral while adding a
deploy-wide free inference tier (an OpenAI-compatible endpoint the
operator configures; users get AI features with zero setup, and
BYOK remains the upgrade path).

Read [`architecture.md`](architecture.md) first if you don't know
the AgentPort seam. This doc is the design spec for the
provider-generalization work: current state with a leak audit,
target architecture, output-contract strategy, UX, rollout, tests,
and a milestone plan.

Scope decisions already made (do not relitigate):

- `AgentPort` (`prep/agent/port.py`) stays the one seam. No new
  port. The goal is purging provider leakage above the adapter
  layer and generalizing selection.
- The free tier is a GENERIC OpenAI-compatible adapter configured
  by env (base URL + key + model + extra body). The current deploy
  target happens to be Hetzner's inference API; that is
  configuration, not code, and the deploy values live in the
  private infra repo.
- Free tier is enabled by default when the deploy configures it.
  No per-user opt-in toggle in v1. The settings page discloses it.
- The clerk-mode hard-gate on the deploy-wide Claude subscription
  token stays exactly as is. See "Two deploy-wide credentials,
  two policies" below; that section exists so nobody "fixes" the
  asymmetry in either direction.
- The worker path is unchanged: HTTP `/api/agent/run` with
  `user_id`; the endpoint resolves the adapter.

---

## 1. Current state

### The seam

`prep/agent/port.py` defines the whole contract:

```
AgentPort.run(prompt, *, model=None, reasoning=None, timeout_s=120.0)
    -> AgentResult(text, model, input_tokens?, output_tokens?,
                   cost_usd?, duration_ms?)

raises AgentUnavailable          (any provider-side failure -> 502)
raises AgentBudgetExhausted      (subclass; quota/credit exhaustion -> 429)
```

Five implementations ship in-tree:

| Adapter | File | Auth | Notes |
| --- | --- | --- | --- |
| `ClaudeAgentSdkAdapter` | `prep/agent/sdk_adapter.py` | subscription OAuth token | in-process `claude-agent-sdk`; deploy-wide or per-user BYOK token |
| `AnthropicApiAdapter` | `prep/agent/anthropic_api.py` | BYOK `sk-ant-api03-` key | direct httpx to the Messages API |
| `OpenAIAdapter` | `prep/agent/openai_api.py` | BYOK `sk-` key | subclass of the compat base |
| `OpenRouterAdapter` | `prep/agent/openrouter.py` | BYOK `sk-or-v1-` key | subclass of the compat base |
| `FakeAgent` | `prep/agent/fake.py` | none | test double |

`prep/agent/openai_compat.py` already speaks the generic
chat-completions wire format (`POST <base>/chat/completions`,
bearer auth, `choices[0].message.content`, `usage.prompt_tokens` /
`completion_tokens`). It is currently subclass-configured via four
class attrs (`_api_base`, `_default_model`, `_prefix_check`,
`_provider_label`), which is the piece the free tier generalizes.

Selection is `prep/agent/selector.py::agent_for_user(user_id)`:

1. test override (`set_user_agent_factory`)
2. per-user BYOK row (user's explicit `active_byok_provider`
   choice, then `_BYOK_PROVIDER_ORDER`)
3. deploy-wide subscription OAuth token, but ONLY when
   `_subscription_path_allowed()` (hard-gated off on
   `PREP_AUTH_MODE=clerk`)
4. `_NoopAgent`, whose `.run()` raises `AgentUnavailable`

Callers never branch on auth shape. The Go worker POSTs
`/api/agent/run` (`{prompt, user_id?, ...} -> {stdout}`,
`X-Internal-Token` gated) and the endpoint runs the selector.

### What is genuinely provider-agnostic today

- The port signature, `AgentResult`, and the exception taxonomy.
  No caller imports an adapter class; services catch
  `AgentUnavailable` / `AgentBudgetExhausted` only.
- The selector shape: one function, precedence-ordered, one noop
  centralizing "AI is not configured."
- The worker wire format: text in, text out, no model vocabulary.
  `worker-go/agent/agent.go` deliberately refuses to model
  messages/roles/params.
- The caller-side parsing: every consumer treats the response as
  freeform text and extracts JSON tolerantly (section 3).
- The compat base adapter: already vendor-shaped only in its
  subclasses, not in its wire handling.

### What is not

- `port.py` hardcodes `DEFAULT_MODEL = "claude-sonnet-4-6"`: the
  provider-agnostic port names a vendor model, and two adapters
  import it as their default.
- "claude" is woven through service naming, user-visible strings,
  workflow statuses, metric names, a route path, and template
  copy. The audit below itemizes it.
- Availability semantics (`agent_available`) only know about BYOK
  rows and the subscription token file; there is no notion of a
  deploy-provided default.

### Leak audit

Every mention of Claude/Anthropic above the adapter layer, from:

```
grep -rniE 'claude|anthropic|sk-ant' prep/ templates/ worker-go/ static/js/ -n
```

(adapter files `sdk_adapter.py`, `anthropic_api.py`,
`openai_api.py`, `openrouter.py`, `openai_compat.py` excluded:
they are the layer that is allowed to name vendors). The Go worker
and static JS are in scope deliberately: the worker writes
user-visible strings into workflow progress fields that templates
render verbatim, so a prep-only grep misses real UI leakage.

**KEEP: legitimately provider-specific.** These name Claude or
Anthropic because the thing itself is Claude- or Anthropic-shaped.
Renaming them would be false neutrality.

| Where | Why it stays |
| --- | --- |
| `prep/byok/entities.py` (23-135) | the provider registry. `Provider` enum values, key prefixes, console URLs, per-provider default models. This is the one blessed place above the adapters that enumerates vendors. |
| `prep/byok/__init__.py:3`, `prep/byok/crypto.py:3,151-154` | docstrings sized to real key shapes; the mask helper's defaults reference `sk-ant-api03-`. |
| `prep/agent/token_store.py` (all), `prep/agent/status.py` (all) | the subscription-OAuth machinery: file name, env var `CLAUDE_CODE_OAUTH_TOKEN`, probe. Inherently that provider's path. |
| `prep/agent/routes.py:154,281-343` | `/settings/agent/connect` validates the `sk-ant-oat01-` setup-token prefix; the clerk refusal copy names the alternatives. Subscription-path surface. |
| `prep/agent/selector.py:93-98,101-126,231-234` | the composition point must import concrete adapters and name `Provider` members. (Docstring wording at 6, 137-147, 160, 172 gets neutral touch-ups in M1, but the code stays.) |
| `prep/app.py:17,460-466` | boot log naming the real env var and the setup-token flow. Operator-facing, accurate. |
| `prep/web/log_redaction.py` (all) | redacts Anthropic-shaped secrets by regex; the patterns ARE the point. (Follow-up noted in section 5: add a pattern for the free-tier key once its shape is known.) |
| `prep/chat_handoff.py:5-25`, `templates/result.html:148,159`, `templates/trivia/card.html:174` | the "Discuss this card" handoff to claude.ai / chatgpt.com as external consumer products. Unrelated to the inference provider prep calls. |
| `prep/api/mcp.py:4`, `prep/api/repo.py:32`, `prep/api/entities.py:8`, `prep/web/legal.py:64`, `templates/settings_api.html:39-219`, `templates/settings_agent.html:245-276` | MCP client documentation: Claude Code / Claude Desktop are real client products the copy teaches users to configure. |
| `templates/settings_agent.html:116-121,145-224` | the claude-subscription BYOK row hint and the deploy-wide subscription panel. Provider-specific rows for a provider-specific credential. |

**PURGE: should be neutral.** These name Claude where the truthful
name is "the AI" / "the model" / "the agent". Purge means reword
or rename, never delete behavior.

| Where | What | Fix |
| --- | --- | --- |
| `prep/agent/port.py:27-28` | `DEFAULT_MODEL` / `DEFAULT_REASONING` vendor values on the port | move into the adapters (each adapter owns its default; BYOK defaults already live in the `PROVIDERS` registry). The port stops naming models. |
| `prep/agent/port.py:6-9,61-68,79` | docstrings: "swap Claude for another provider", `AgentBudgetExhausted` described as the Anthropic credit pool | reword: the exception means "the configured credential's quota/credit pool is exhausted", any provider. |
| `prep/trivia/service.py:392,481` | `claude_grade` / `claude_regrade` function names | rename `ai_grade` / `ai_regrade` (thin aliases kept one release for imports). |
| `prep/trivia/service.py:316` | `_CLAUDE_GRADE_PROMPT` | `_AI_GRADE_PROMPT`. |
| `prep/trivia/service.py:287-313,547` | `classify_grading` returns the string `"claude"` | return `"ai"`. Internal value; one call site. |
| `prep/trivia/service.py:441,473` | USER-VISIBLE fallback feedback: "graded by string similarity - claude was unreachable / returned malformed JSON" | "the AI grader was unreachable" / "the AI grader returned malformed output". |
| `prep/trivia/service.py` misc docstrings (11, 42, 143, 269-303, 404-415, 487, 527-540) | narration naming claude | reword. |
| `prep/trivia/routes.py:96,561,574,622,625,750` | `claude_regrade` call sites + comments | rename with the service. |
| `prep/web/metrics.py:72-86` | `prep_claude_grade_duration_seconds` + `observe_claude_grade` | rename to `prep_ai_grade_duration_seconds` / `observe_ai_grade`. Operator-facing telemetry: the Grafana panel query changes in the obs stack (one line, infra-side) and lands together with the M1 deploy, not later, so the panel is never dark; historical series stays under the old name. |
| `prep/workflows/entities.py:81,165-166` | workflow status `"asking_claude"`, displayed "asking claude" | `"asking_ai"` / "asking AI". Nothing emits this status today (the Go trivia workflow emits `"generating"`), so this is dead display tolerance: rename the literal and the mapping, no read-compat machinery needed. |
| `worker-go/workflows/trivia.go:97` | USER-VISIBLE: `progress.Error = "claude returned 0 cards"`, rendered verbatim by the trivia progress partial | "the AI returned 0 cards". |
| `worker-go/workflows/plan.go:56` | "the deck description seeds claude" inside a NonRetryable error message that surfaces in the workflow error state | reword neutral. |
| `prep/trivia/scheduler.py:144`, `prep/decks/entities.py:164,170`, `prep/decks/routes.py:1093`, `prep/trivia/service.py:206-211` | comment-only mentions | reword. |
| `static/js/modules/improve-dialog.js:2`, `static/js/modules/copy-button.js:22` | comment-only mentions | reword. |
| `prep/decks/routes.py:668-669`, `templates/partials/deck_overflow_menu.html:65` | USER-VISIBLE route `/deck/{name}/edit-with-claude` | rename to `/deck/{name}/edit-with-ai`; keep the old path as a redirect (bookmarks, PWA history). Template already renders `deck_edit_ai.html`. |
| `templates/settings_agent.html:17-23` | page lede framing AI as strictly bring-your-own | new copy per section 4: free AI included by default (when deploy-configured), "bring your own Claude" as the upgrade. |
| `prep/web/templates.py:41,44` | docstring "users without claude installed" | reword. |
| `prep/temporal_client.py:126,191,246` | docstrings "claude plans/returns..." | reword "the model". |
| `prep/decks/service.py:314,355,461,495`, `prep/decks/repo.py:281,577,652` | comments "claude sees / claude proposes" | reword. |
| `prep/infrastructure/db.py:304,375,386-392` | schema comments naming claude as the generator | reword. (`:476` is a historical note about Anthropic metering on a dropped table: keep, it explains a tombstone.) |
| `prep/domain/grading.py:9,16,18,31,91,109,120` | the PURE domain layer narrates claude | reword "the AI grader" / "model-proposed regex". Domain code should not know the vendor exists. |
| `prep/trivia/__init__.py:13`, `prep/trivia/agent_client.py:5,53` | docstrings | reword. |

The purge is mostly renames and comment rewording: zero behavior
change, shippable on its own (milestone M1). The user-visible
items (the fallback feedback strings, the worker's trivia/plan
error strings, the `asking_claude` display, the
`edit-with-claude` URL) are the ones that need care.

---

## 2. Target architecture

### The generic OpenAI-compatible adapter

`OpenAICompatAdapter` (`prep/agent/openai_compat.py`) is promoted
from subclass-only to directly constructible. No vendor name in
the class; a vendor is a set of constructor arguments.

```python
OpenAICompatAdapter(
    api_key,                       # bearer token
    *,
    base_url=None,                 # e.g. "https://api.openai.com/v1";
                                   # falls back to the class attr for
                                   # the existing BYOK subclasses
    model=None,                    # default model; falls back to class attr
    extra_body=None,               # dict merged into the request body
                                   # (deploy knobs like sampler / reasoning
                                   #  switches; caller args win over it,
                                   #  it wins over adapter defaults)
    shared=False,                  # True = deploy-wide shared credential:
                                   # changes 429/timeout mapping, see below
    max_tokens=4096,
    provider_label="AI",           # used in error messages only
    transport=None,                # httpx transport injection for tests
)
```

- `OpenAIAdapter` / `OpenRouterAdapter` keep working unchanged
  (class attrs remain the defaults; their key-prefix checks stay).
- The prefix check INVERTS its empty-tuple meaning. Today the
  guard is
  `not any(key.startswith(p) for p in self._prefix_check)`, and
  `any()` over an empty tuple is False, so an empty tuple REJECTS
  every key. M2 must change the constructor so an empty prefix
  tuple means SKIP validation entirely (the free-tier key shape is
  not ours to validate), while non-empty tuples keep rejecting
  mismatches. Do not implement "empty = skip" by leaving the
  current guard in place; a contract test pins both meanings
  (section 6).
- Request body: `{model, max_tokens, messages}` merged with
  `extra_body`. `extra_body` must not be able to override
  `messages`; log-and-drop that key defensively.
- **No streaming in v1.** The current adapter does a single POST
  and so does this one. Streaming is a port-widening decision for
  another day.
- **Timeout policy unchanged:** the port's `timeout_s` passes
  straight to the httpx client, defaulting 120s; grading keeps its
  12s cap (`prep/trivia/service.py::_GRADE_TIMEOUT_S`), generation
  keeps 900s (`prep/trivia/agent_client.py`).

Error mapping:

| Upstream | `shared=False` (BYOK) | `shared=True` (free tier) |
| --- | --- | --- |
| 401 / 403 | `AgentUnavailable` ("auth rejected") | `AgentUnavailable` (operator misconfig: the deploy key is bad; log loudly) |
| 429, or a quota-coded error body at any status | `AgentBudgetExhausted` (the USER's key/quota) | `AgentBusy` (shared key contention, not the user's fault) |
| timeout | `AgentUnavailable` | `AgentBusy` |
| other non-200, non-JSON, empty content | `AgentUnavailable` | `AgentUnavailable` |

Shared-mode note: the upstream 429 covers two different time
scales (minute-scale rate limiting AND the daily token cap) and
the response does not reliably say which, so `AgentBusy` copy
must not promise a specific recovery time (section 4). The
quota-coded-body row is deliberate: the existing mapper reads any
"quota" type/code as budget exhaustion, which is correct for a
user's own key and wrong for the shared one; in shared mode the
same signals map to `AgentBusy`.

`AgentBusy(AgentUnavailable)` is a new exception in
`prep/agent/port.py`: "the shared free-tier capacity is saturated;
retry later or add your own key." Subclassing
`AgentUnavailable` keeps every existing catch-site working;
callers that want the distinct UX catch it first.
`/api/agent/run` maps it to `429 {"error": ..., "kind":
"free_tier_busy"}`, parallel to the existing
`"budget_exhausted"` kind. (The Go worker passes the error string
through today; teaching its retry policy to back off on the
`kind` field is a deliberate non-goal for v1: a busy free tier
surfaces as the normal workflow error state.)

### Free-tier configuration

Provider-neutral env names. The free tier is configured iff the
first three are all set:

| Env var | Meaning |
| --- | --- |
| `PREP_FREE_INFERENCE_BASE_URL` | OpenAI-compatible API base, e.g. `https://<provider>/api/v1` |
| `PREP_FREE_INFERENCE_API_KEY` | the deploy-wide key |
| `PREP_FREE_INFERENCE_MODEL` | model identifier the endpoint expects |
| `PREP_FREE_INFERENCE_EXTRA_BODY` | optional JSON object merged into every request body. Parsed ONCE at factory time, never per request; a parse failure disables the free tier (see the factory contract below). |

A small factory in the selector module:

```python
def free_tier_agent() -> AgentPort | None:
    # None unless BASE_URL + API_KEY + MODEL are all set.
    # Returns OpenAICompatAdapter(..., shared=True,
    #                             provider_label="free AI").
    # NEVER RAISES: any config failure logs at ERROR and
    # returns None (free tier off). See below.
```

**The factory is never-raising by contract.** `agent_for_user`
runs inside the Jinja context processor on every page render (via
`agent_available_for_user`), so an exception escaping the factory
is not a degraded AI feature: it is a 500 on every page of the
deploy. One malformed value in the deploy secret must mean "free
tier off", never an outage. Every failure inside the factory is
therefore caught and converted:

- `PREP_FREE_INFERENCE_EXTRA_BODY` that does not parse as a JSON
  object: ERROR log naming the var, return None.
- A key or config shape the adapter constructor rejects, or any
  other constructor exception: ERROR log, return None.
- The three required vars partially set: ERROR log naming the
  missing ones (a half-configured free tier is operator error and
  should not be silent), return None.

The log is loud and operator-actionable; the user impact is only
"free tier absent" (the selector falls through to Noop). A
contract test pins every branch, including "the factory never
raises" itself (section 6).

Hetzner is the current deploy target of this configuration, not
code: their inference API is OpenAI-compatible, free, and
rate-limited per key (60k output tokens/min, 5M/day, HTTP 429
beyond). Provider facts worth recording here because they shaped
the design (verified against the live API), while the actual
deploy values live in the private infra repo, never in this repo:

- The API serves four models: DeepSeek-V4-Flash-0731 (512K
  context), GLM-5.2-NVFP4 (512K), Kimi-K2.7-Code (262K), and
  Qwen/Qwen3.6-35B-A3B-FP8 (262K).
- The chosen default is DeepSeek-V4-Flash-0731: measured 5.6s
  wall on a grading-shaped strict-JSON call, clean JSON on the
  first try, comfortable under grading's 12s cap. Qwen3.6-35B
  measured 1.7s on the same call, also clean; it is the
  documented latency fallback if grading ever gets tight under
  the cap. Swapping is a `PREP_FREE_INFERENCE_MODEL` change, no
  code.
- API keys are minted console-only (no API), so rollout gates on
  the operator pasting a key into the deploy secret.
- Reasoning ("thinking") is ON by default upstream and silently
  eats the completion budget before any answer tokens arrive.
  Disabling it is verified working via the request body
  `{"chat_template_kwargs": {"enable_thinking": false}}`; note
  the flag nests under `chat_template_kwargs`, it is not a
  top-level field. This is exactly the class of knob
  `PREP_FREE_INFERENCE_EXTRA_BODY` exists for.

### Selection precedence

`agent_for_user(user_id)` becomes:

1. test override
2. per-user BYOK (unchanged: user's own key always wins; nobody
   is silently downgraded to the shared free model after
   configuring their own provider)
3. deploy-wide subscription OAuth token, single-user installs
   only (`_subscription_path_allowed()` unchanged)
4. **free tier, when configured (NEW)**
5. `_NoopAgent`

On clerk deploys step 3 is inert, so the effective order is BYOK,
else free tier, else unavailable: which is the operator directive
verbatim. On tailscale single-user installs the operator's own
subscription token outranks the free tier (better model, the
operator opted into it, and the flat-rate pool is theirs to burn).

`user_id=None` (system-initiated calls) skips BYOK as today and
may land on the free tier.

### BYOK failure is not a downgrade path (READ THIS)

BYOK is the privacy opt-out: section 4's disclosure tells users
"add your own key" precisely so their prompts stop going to the
shared third-party endpoint. A user who configured a key, and
whose key path then FAILS, must never be silently served by the
free tier.

Today the selector wraps its whole BYOK step in a broad
`except Exception` and falls through, and the BYOK repo
deliberately propagates decrypt/master-key errors (they signal a
config problem, not a missing row). Pre-free-tier, that
fall-through lands on Noop on clerk deploys: a visible "AI
unavailable" error. With free tier as step 4 the same
fall-through would land on the shared endpoint: a silent privacy
downgrade that directly contradicts the step 2 guarantee above.
The selector change must make the free tier unreachable from the
BYOK-exception path:

- Only "no BYOK row for this user" continues past step 2.
- Any exception inside the BYOK step (row lookup failure, key
  decrypt failure, adapter construction failure, anything
  unexpected) logs loudly and returns `_NoopAgent` immediately.
  The user sees "AI unavailable", the operator sees the config
  error, and nobody's prompts change provider without their own
  action.

On clerk deploys this preserves today's observable behavior (the
fall-through landed on Noop anyway). On single-user tailscale
installs it is a deliberate change: a broken BYOK row used to
fall through to the subscription token; now it surfaces as an
error, because silently switching credentials masks the broken
key. Ships with the selector change in M3, pinned by the test:
BYOK row present + decrypt raises + free tier configured -> Noop,
NOT the free adapter.

### Two deploy-wide credentials, two policies (READ THIS)

This is a deliberate asymmetry. Do not "fix" it in either
direction.

- **`CLAUDE_CODE_OAUTH_TOKEN` (subscription path): hard-gated OFF
  on multi-user clerk deploys.**
  `prep/agent/selector.py::_subscription_path_allowed()` exists
  because that token draws from the OPERATOR'S PAID Claude
  subscription pool. On a public deploy, every random signup
  would silently spend the operator's money. The gate stays, the
  connect-route refusal stays, the template hiding stays. The
  free tier is NOT a precedent for relaxing any of it.
- **`PREP_FREE_INFERENCE_API_KEY` (free tier): deliberately
  deploy-wide, clerk mode included.** The key is free and
  rate-limited per key upstream. It is not operator-funded; the
  worst abuse outcome is degraded availability (429s for
  everyone on the deploy), not a bill. That failure mode is
  handled as UX (`AgentBusy` -> "try again or add your own
  key"), not as an auth gate. Do not wrap this key in the clerk
  gate "for consistency": that would delete the entire feature
  (zero-setup AI for public signups) to prevent a cost that
  cannot occur.

If either premise changes (the free provider starts charging, or
Anthropic offers a free operator pool), revisit the matching
policy, not the other one.

### Availability semantics

`agent_available` (the Jinja flag gating every AI surface) must
reflect the free tier:

- `selector.agent_available_for_user(uid)` already returns "would
  the selector hand back a non-noop adapter", so it inherits the
  new step 4 for free once the selector changes.
- `prep/agent/__init__.py::is_available_for` does NOT simply
  agree today. Its deploy-wide short-circuit reads the
  subscription status probe, which is env/file presence only and
  not clerk-gated: a stray `CLAUDE_CODE_OAUTH_TOKEN` on a clerk
  deploy makes `is_available_for` report True while the selector
  hands back Noop. Pre-existing skew, not introduced by the free
  tier, but M3 touches this function anyway: make the
  short-circuit delegate to the selector's own gating (or to
  `agent_available_for_user`) instead of the raw probe. Do not
  just assert the two agree; today they don't.
- The template-context fallback for requests with no resolved
  user (error pages) does not consult the selector and will not
  reflect the free tier there. Cosmetic; noted, not v1 work.
- `prep/agent/status.py::status()` stays the subscription-path
  probe, but the settings route needs a richer availability
  breakdown to render section 4's states: which of
  {byok, subscription, free} are live. Add a
  `free_tier_configured() -> bool` helper next to the factory and
  pass it into the settings template context; don't overload the
  legacy `status()` dict shape that other callers pin.

---

## 3. Output contract: a different model, same parsers

prep's AI flows expect structured JSON in freeform text. Claude
has been the only producer; the free-tier models (DeepSeek
default, Qwen latency fallback, per section 2) have different
habits: chattier preambles, different fencing, and (if not
disabled) reasoning traces in the content. The strategy: **tolerance stays where it already lives,
in the caller-side parse helpers; the adapter stays dumb text
transport; contract tests pin both against recorded shapes.**

Existing tolerance + fallback inventory (these are the seams that
already absorb Claude's quirks, and the ones fixtures must pin):

Python:

- `prep/trivia/service.py::_parse_qa_pairs` (142-161): strips
  code fences, grabs first `[` to last `]`. Failure raises
  `AgentUnavailable`: generation surfaces an error, never inserts
  bad cards.
- `prep/trivia/service.py::_parse_grade_json` (371-380) inside
  `ai_grade`: bad JSON falls back to the deterministic string
  match with the `fallback_bad_json` metric label (462-475);
  adapter failure falls back with `fallback_unavailable`
  (429-443). Grading never hard-fails on model output.
- `prep/domain/grading.py::validate_regex_update`: any
  model-proposed regex must compile and match both the canonical
  answer and the user's answer, else it's dropped to None.

Go (worker activities; each failure surfaces as an activity error
in the workflow UI rather than corrupt data):

- `worker-go/activities/activities.go::parseCardJSON` (84+):
  fences, leading/trailing prose, a loose re-decode for
  wrong-typed fields, required-field check.
- `worker-go/activities/grading.go::parseVerdictJSON` (226+):
  fences + brace-bounding; unknown verdicts coerce to "wrong"
  (fail-safe direction); missing model answer backfilled.
- `worker-go/activities/plan.go::extractJSON` (174+) +
  `parsePlanJSON`: the most battle-tested extractor (three
  observed wild shapes documented in-line).
- `worker-go/activities/trivia.go::parseTriviaJSON` (238+).

What changes for a non-Claude model:

- **Reasoning traces are the new failure shape.** A `<think>...`
  block or an upstream `reasoning_content` field either breaks
  the first-brace heuristics or burns the `max_tokens` budget so
  the JSON truncates mid-object (which every parser above
  correctly rejects, cascading to fallbacks). Primary mitigation
  is configuration: the deploy's `extra_body` disables thinking
  upstream (verified working against the live endpoint, section
  2). Defensive mitigation: the compat adapter strips one
  leading `<think>...</think>` block from the content before
  returning, IF recorded fixtures show the model emitting them
  even when disabled. Do not add speculative stripping the
  fixtures don't justify.
- **Truncation.** 4096 `max_tokens` has headroom for grading and
  per-card calls, and the 25-card trivia batch (explanations +
  regexes) is tight but bounded. The true worst case is the
  deck-wide transform: its modification list can carry old and
  new content for every card in a deck, so output scales with
  deck size, unbounded by any batch constant. The SDK adapter
  imposes no comparable output cap, which makes a capped compat
  adapter a NEW regression class: transform output truncates
  mid-object, the parse fails, the flow dies as an activity
  error. Plan: the free-tier factory sets a transform-safe
  `max_tokens` well above 4096 via constructor arg (the output
  cap is ours to choose; the endpoint models carry 262K to 512K
  context), sized from the section 6 budget smoke, which must
  include BOTH a full-cap trivia batch AND a transform-shaped
  output at a realistic large-deck size. A deck that still cannot
  fit surfaces the normal activity error; a per-flow cap or
  chunked transforms is the noted follow-up, not v1. Do not ship
  the free tier with a 4096 cap on the transform path.
- **Instruction adherence.** "Return ONLY valid JSON" compliance
  will differ. That is precisely what the tolerant extractors are
  for; the contract tests turn "we think they cope" into pinned
  behavior.

Contract fixtures (`tests/agent/fixtures/openai_compat/`): small
recorded response bodies, checked in, named by shape:
`happy.json`, `fenced.json`, `preamble.json`, `think_tag.json`,
`truncated.json`, `rate_limited_429.json`, `auth_401.json`,
`empty_content.json`. Record them once against the real endpoint
during M5 staging validation (redact nothing but the key; the
bodies are model output over synthetic prompts). Python parse
helpers and Go table tests both consume the same shapes.

---

## 4. UX: settings page and error surfaces

### /settings/agent states

The page renders one of four states, driven by
`free_tier_configured()` x "user has any BYOK row":

| State | Render |
| --- | --- |
| free only (free configured, no BYOK) | "Free AI included" callout at the top: AI features work now, no setup. BYOK rows below framed as the upgrade ("your own key, your own model, no shared rate limit"). |
| free + BYOK | BYOK row active as today, plus a quiet line on the callout: "Your own key is active and takes priority. Remove it to fall back to the included free AI." |
| BYOK only (free not configured) | current behavior, current copy. |
| none | current "not configured" behavior: AI surfaces hidden, page explains the options. |

Copy goes provider-neutral: the lede stops implying AI is
strictly bring-your-own. Shape (final wording at implementation):
"AI features are included by default on this deploy, powered by a
free shared model. Bring your own Claude (or OpenAI / OpenRouter
key) to upgrade." Vendor names stay in the BYOK rows where they
are truthful; the free tier is described as "free AI", with the
provider named in the disclosure line below.

### Disclosure (required, v1)

The free-tier callout carries a disclosure, always rendered when
the free tier is configured:

> Free AI runs on a third-party inference service (currently
> Hetzner's experimental API). Prompts and card content you send
> through AI features leave this deploy and are processed there.
> The provider publishes no data-use or retention statement for
> this API. If that's not acceptable, add your own API key below:
> your key means your provider, your terms.

No per-user disable toggle in v1 (operator decision: default on;
BYOK is the opt-out). If a toggle ships later it belongs on this
page next to the disclosure.

### The busy error

`AgentBusy` surfaces distinctly everywhere a generic failure
would otherwise show:

- `/api/agent/run` returns `429 {"kind": "free_tier_busy"}`.
- Route/UI copy: "Free AI is busy right now (it's shared by
  everyone on this deploy). Try again later, or add your own key
  in Settings for dedicated capacity." Link to
  `/settings/agent`. The copy promises no recovery time on
  purpose: the upstream 429 covers both minute-scale rate
  limiting and the daily cap, and the response does not say which
  (section 2).
- Trivia grading: unchanged mechanics; `AgentBusy` is an
  `AgentUnavailable`, so the string-match fallback fires and the
  feedback line reads "(graded by string similarity - free AI
  was busy)".
- Workflow flows (plan, transform, trivia generation): the
  activity error string carries the busy message into the
  workflow's `progress.Error`, rendered by the existing error
  surfaces. Smarter worker-side backoff on `kind` is a noted
  follow-up, not v1.
- Study grading is the exception, and the gap must close:
  `GradeProgress` has no `Error` field, the grade workflow only
  flips its status to failed, and the grading template renders a
  generic failure hint, so the busy message (and its "add your
  own key" pointer) dies inside the worker before the user sees
  it. M4 adds `Error` to `GradeProgress` (worker shared types +
  the grade workflow's failure path) and renders it in the
  grading progress template, matching the other flows.

---

## 5. Config + rollout

### Env and secrets

New (all optional; all unset = feature off = today's behavior):

```
PREP_FREE_INFERENCE_BASE_URL=
PREP_FREE_INFERENCE_API_KEY=
PREP_FREE_INFERENCE_MODEL=
PREP_FREE_INFERENCE_EXTRA_BODY=
```

- This repo: names + comments in `.env.example` only. Values
  never land here.
- Private infra repo: each environment's deploy secret gains the
  four keys; the deploy docs there record the actual values (base
  URL, model id, extra body). Staging and prod use SEPARATE keys
  (never share secrets across environments; also keeps one
  environment's rate-limit burn from starving the other).
- Follow-up once the key shape is known: add a redaction pattern
  to `prep/web/log_redaction.py` so a pasted/logged free-tier key
  is scrubbed like the Anthropic shapes are.

### Staged rollout

1. Ship the milestones (section 7) with the env unset everywhere.
   Every tag is deployable; behavior is identical to today until
   the key exists. (Release discipline as always: merge to main,
   tag, THEN build/deploy.)
2. Operator mints a key in the provider console (console-only)
   and adds the four values to the staging Secret.
3. Staging validation: the real-API smoke (section 6), plus
   manual passes of trivia generation, a free-text grade, a plan
   flow, a large-deck transform, and the settings page in all
   four states (flip states by removing/adding a BYOK key on a
   test account).
4. Record the contract fixtures from staging traffic shapes.
5. Operator mints the prod key, promotes the same
   staging-validated tag through the standard promote flow, sets
   the prod secret, verifies the smoke against prod.

Rollback is config: unset the env, free tier disappears, BYOK and
subscription paths are untouched.

---

## 6. Test plan

Layered like the rest of the suite (`make ci`); no real network
below the smoke layer.

1. **Adapter contract tests**
   (`tests/agent/test_openai_compat.py`), against a mock server
   via httpx transport injection (the `transport=` constructor
   arg; `httpx.MockTransport` handlers, no socket):
   - request shape: bearer header, model, `max_tokens`,
     `extra_body` merged (and `messages` not overridable by it)
   - prefix-check semantics: an empty prefix tuple accepts any
     key shape; the BYOK subclasses' non-empty tuples still
     reject mismatches (pins the M2 inversion, section 2)
   - happy-path parse: text, `prompt_tokens`/`completion_tokens`
   - error mapping table from section 2, BOTH modes: 429 with
     `shared=False` -> `AgentBudgetExhausted`, with `shared=True`
     -> `AgentBusy`; a quota-coded error body respectively
     `AgentBudgetExhausted` / `AgentBusy`; timeout respectively
     `AgentUnavailable` / `AgentBusy`; 401 -> `AgentUnavailable`
     both
   - degenerate bodies: non-JSON, empty `choices`, empty content
   - fixture-driven: each recorded shape from section 3 either
     parses or raises the pinned exception
2. **Free-tier factory contract** (with the selector tests): env
   unset -> None; all three set -> shared-mode adapter; malformed
   `PREP_FREE_INFERENCE_EXTRA_BODY` -> None plus an ERROR log
   (caplog); a constructor that raises -> None plus an ERROR log;
   partial config -> None plus an ERROR log. The property under
   test is section 2's contract: the factory NEVER raises,
   because it runs on every page render.
3. **Selector precedence** (extend
   `tests/byok/test_selector.py`): free configured + no BYOK ->
   compat adapter in shared mode; BYOK beats free; **BYOK row
   present + key decrypt raises + free configured -> Noop, NOT
   the free adapter** (the privacy pin from section 2); clerk
   mode + stray `CLAUDE_CODE_OAUTH_TOKEN` + free configured ->
   free (the gate still wins over the token); tailscale + token +
   free -> SDK adapter (subscription outranks free); nothing ->
   noop; `agent_available_for_user` true on free-only.
4. **Route pins** (extend `tests/agent/test_api_run.py`):
   `AgentBusy` from the adapter -> 429 + `kind: free_tier_busy`;
   existing `budget_exhausted` and 502 paths unchanged.
5. **UI pins** (extend `tests/agent/test_routes.py`): settings
   page renders the four states; disclosure text present whenever
   the free tier is configured; `agent_available` template flag
   true with free tier and no BYOK.
6. **Parse-helper fixture tests**: Python
   (`_parse_qa_pairs`, `_parse_grade_json`,
   `validate_regex_update`) and Go table tests
   (`parseCardJSON`, `parseVerdictJSON`, `extractJSON`,
   `parseTriviaJSON`) consume the recorded shapes; damaged shapes
   pin the documented fallback (raise / string-match / coerce
   wrong / activity error), never silent bad data.
7. **Real-API smoke** (`tests/e2e/test_free_inference_smoke.py`),
   `pytest.mark.skipif` on `PREP_FREE_INFERENCE_API_KEY` being
   unset, marked `slow`: one trivia-shaped prompt through the
   real adapter asserting the output survives `_parse_qa_pairs`;
   one full-cap trivia batch AND one transform-shaped output at a
   realistic large-deck size to prove (and size) the raised
   output cap (section 3); one grade-shaped prompt through
   `_parse_grade_json`. This is the only test that touches the
   real endpoint, and it never runs in CI without the
   operator-provided key.

Purge regression guard: a lightweight test greps rendered
user-facing surfaces (the fallback feedback strings, workflow
status display) for `claude` to keep the neutral wording from
regressing. Rendered templates alone cannot catch worker-emitted
text (the worker writes `progress.Error` strings that templates
render verbatim), so the guard also covers the worker: grep the
string literals in `worker-go/workflows/` and
`worker-go/activities/` that feed progress/error fields. Don't
grep the whole tree (the KEEP list is legal).

---

## 7. Build plan

Each milestone merges to main, tags, and is independently
deployable (the feature stays dark until the env lands in step 2
of the rollout).

- **M1: neutralize above the adapters.** The purge table:
  renames (`ai_grade`, `_AI_GRADE_PROMPT`, `"ai"` mode,
  `asking_ai`, `observe_ai_grade` + metric rename,
  `/edit-with-ai` + redirect), user-visible string fixes in BOTH
  languages (the Python fallback feedback strings AND the Go
  worker's trivia/plan error strings), docstring/comment
  rewording, `DEFAULT_MODEL` relocation into the adapters,
  `AgentBusy` added to the port (unused yet). The obs-side
  Grafana panel query update lands together with this
  milestone's deploy so the metric rename never darks the panel.
  Zero behavior change; the whole existing suite stays green
  plus the purge-guard test.
- **M2: the generic adapter.** Constructor contract on
  `OpenAICompatAdapter` (base_url/model/extra_body/shared/
  transport), empty-prefix-tuple = skip validation (inverting
  today's reject-all meaning, section 2), shared-mode error
  mapping, transport injection. BYOK subclasses byte-identical
  in behavior. Contract tests.
- **M3: selection + endpoint.** Never-raising
  `free_tier_agent()` factory, selector step 4, BYOK-exception
  isolation (free tier unreachable on BYOK failure, section 2),
  availability semantics (incl. aligning `is_available_for` with
  the selector's gating), `/api/agent/run` busy mapping.
  Selector + factory + route tests. Deploys dark (env unset).
- **M4: settings UX + error surfaces.** Four states, neutral
  lede, disclosure copy, busy-error copy, `GradeProgress.Error`
  + the grading template line (study grading stops swallowing
  the failure message, section 4). UI pins.
- **M5: fixtures + validation.** Staging key, real-API smoke
  (incl. the transform-shaped budget run that sizes the raised
  output cap), record fixtures, Go table tests, then prod
  promote per the rollout.

M1 is independent and can land first while the operator sorts the
key. M2 and M3 are the core and could collapse into one release
if review prefers; keeping them split keeps each diff reviewable.
