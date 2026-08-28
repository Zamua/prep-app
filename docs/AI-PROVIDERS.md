# AI providers

How prep talks to LLMs: the seam, the two funding tiers, the rules that
decide which one pays for a call, and the output contract that lets a
different model drive the same parsers.

Read [`architecture.md`](architecture.md) first if you do not know the
`AgentPort` seam.

---

## 1. The seam

Every AI call in the app goes through one port:

```ts
interface AgentPort {
  complete(request: AgentRequest): Promise<string>;
}
```

It takes a prompt and returns text. It knows nothing about providers,
models, JSON, or retries. Everything above it is provider-agnostic by
construction, and the layering test keeps it that way: `app/` cannot
contain `fetch(`, so a use case physically cannot reach a vendor.

Three things live below the port, in `runtime/adapters/agents/`:

| file | what it is |
| --- | --- |
| `anthropic.ts` | the Anthropic Messages API |
| `openaiCompat.ts` | any OpenAI-compatible chat-completions endpoint |
| `byok.ts` | picks one of the two for a user's stored key, and owns the endpoint and attribution headers |
| `freeTier.ts` | the deploy's shared OpenAI-compatible endpoint, read from env |
| `select.ts` | turns one `AgentConfig` into the adapter that serves it |

Adding a provider is a new adapter file plus a row in the catalogue
(`app/settings/providers.ts`, which carries labels, accepted key
prefixes, console URLs and default models). No business code names a
provider.

---

## 2. Two funding tiers

**BYOK.** The user's own API key for Anthropic, OpenAI, or OpenRouter,
AES-256-GCM encrypted in their own cell. Their key, their provider,
their terms, their bill.

OpenRouter additionally supports an OAuth PKCE sign-in
(`app/settings/openrouter.ts` plus `runtime/adapters/openrouter.ts`):
prep redirects, OpenRouter mints a key on the user's own account, and
the callback exchanges the code for it. The key is then stored exactly
like a pasted one. It is the one provider where BYOK needs no
copy-paste.

**Shared free tier.** One OpenAI-compatible endpoint the deploy
configures by env (`PREP_FREE_INFERENCE_BASE_URL`, `_API_KEY`,
`_MODEL`, and `_EXTRA_BODY` for endpoint-specific switches). Users get
AI with zero setup. A deploy that configures nothing simply has no
shared tier, and AI calls refuse with one clear reason.

**There is no third tier, and specifically no deploy-wide subscription
credential.** A Claude Code OAuth token is rejected by the Messages API
and the one sanctioned path for it bundles and spawns a large
executable per call. A row from that retired provider still renders on
the settings page so its owner can delete it and paste an API key
instead; nothing will select it.

### Selection precedence

`app/agent/funding.ts` decides, from the user's own rows, which tier
funds a call. It is app-layer on purpose: it is policy over rows, names
no adapter, and returns a value the composition root turns into an
adapter.

1. **BYOK**, if the user holds any credential row. When several are
   held and none is marked active, the order is Anthropic, OpenRouter,
   OpenAI. Anthropic leads because the prompts were written against
   that model surface.
2. **Shared free tier**, if the deploy configured one.
3. **Refuse**, with a reason naming what the user can do about it.

An anonymous account never reaches step 1 or 2 through this path. Its
one AI route is the instant-generation endpoint, which resolves the
free tier directly.

### BYOK failure is not a downgrade path (read this)

A user who configured their own key, and whose key path then fails,
must **never** be silently served by the shared tier. BYOK is the
privacy opt-out: the whole point of adding a key is that prompts stop
going to the shared third-party endpoint.

So the rules in `select.ts` are:

- Only "this user holds no BYOK row at all" continues past step 1.
- Any failure inside the BYOK step (no master key, decrypt failure,
  unsupported provider, anything unexpected) logs loudly and returns a
  refusing agent immediately, with the `BYOK_UNUSABLE` reason. The user
  sees "re-add your key"; the operator sees the error in the logs; and
  nobody's prompts change provider without their own action.
- `hasByokRows` fails **closed**: if the row lookup itself throws, the
  answer is "yes, they have one", because a shared credential must
  never quietly stand in for a key that might exist.

This is pinned by tests. Do not relax it for convenience.

### Where the credential is read

`SelectedAgent` resolves the config **per call**, never per activation.
The key is decrypted in the isolate that will use it and is not held
past the call. A credential revoked mid-job therefore stops the next
step rather than running to completion on a stale secret.

---

## 3. Availability

One question, asked in one place: would the selector hand back a
non-refusing adapter for this user? Every AI surface in the UI is gated
on that answer, and `/settings/agent` renders a breakdown of which of
{BYOK, shared tier} are live so the page can say something specific.

The refusal reasons are constants in `app/agent/funding.ts`, so
"AI is not configured" is expressed exactly once and no caller needs a
pre-check:

- `NO_FUNDING`: no key, no shared tier.
- `ANON_NO_AGENT`: a guest asking for a path guests do not have.
- `BYOK_UNUSABLE`: rows exist, none of them worked.

---

## 4. Contention, not cost

A shared credential changes what a 429 means. `openaiCompat.ts` carries
a `shared` flag, and that flag is the whole mode split:

- **shared: a 429 or a quota-coded body is contention.** It raises
  `AgentBusy`, whose message says the tier is shared and points at
  Settings for dedicated capacity. A transport timeout on a shared call
  raises `AgentTimeout`, a subclass of `AgentBusy`.
- **BYOK: the same status is the user's own quota**, and surfaces as
  `AgentBudgetExhausted` or a plain failure.

`AgentBusy` is an `AgentUnavailable`, so every flow that already
degrades on "no AI" degrades correctly: trivia grading falls back to
the deterministic string match, and the job flows carry the message
into the step's error, which the progress partials render.

The shared tier's failure mode is degraded availability, not a bill.
That is why it is deploy-wide even on a public multi-user deploy, and
why it is handled as UX rather than as an auth gate.

---

## 5. Output contract: a different model, same parsers

Every AI flow expects structured JSON inside freeform text. Different
models have different habits: chattier preambles, different fencing,
reasoning traces in the content. The strategy is:

> **Tolerance lives in the caller-side parse helpers. The adapter is
> dumb text transport. Fixtures pin both.**

The extractors are in `app/jobs/`, shared by every workflow that reads
a model's output:

- `plan.ts::extractJson` takes a fenced block if there is one, else the
  outermost `{...}` or `[...]`, else the string itself. It is the most
  battle-tested of them.
- `plan.ts::parseCardJson` re-decodes wrong-typed fields loosely and
  then requires the fields it needs.
- `grade.ts` coerces an unknown verdict to "wrong", the fail-safe
  direction, and backfills a missing model answer.
- `domain/grading/regex.ts::validateRegexUpdate` drops any
  model-proposed regex that does not compile, or that fails to match
  both the canonical answer and the user's answer.

A parse failure is never a silent bad write. Generation surfaces an
error rather than inserting malformed cards; grading falls back to the
deterministic matcher and records which fallback fired
(`fallback_bad_json` vs `fallback_unavailable` on the
`prep_ai_grade_duration_seconds` metric).

### The two failure shapes worth naming

**Reasoning traces.** A `<think>` block or an upstream
`reasoning_content` field either breaks the first-brace heuristic or
burns the token budget so the JSON truncates mid-object. The primary
mitigation is configuration: the deploy disables thinking upstream via
`PREP_FREE_INFERENCE_EXTRA_BODY`. Do not add speculative stripping in
the adapter that recorded fixtures do not justify.

**Truncation.** Output caps are ours to choose, and they differ by
call:

| path | cap |
| --- | --- |
| BYOK | 4096, a response-length cap rather than a budget |
| shared tier, general | 32768 |
| shared tier, instant generation | 1024 |

The deck-wide transform is the case that motivated the large shared
cap: its modification list can carry old and new content for every card
in a deck, so output scales with deck size and is not bounded by any
batch constant. A cap sized for grading truncates it mid-object, the
parse fails, and the step dies. If you add a flow whose output scales
with user data, size its cap deliberately.

### What pins all this

Two layers, and they answer different questions.

- **Response shapes** are pinned in `worker/tests/agents.test.ts`: a
  captured `fetch` returns a hand-built body and the test asserts what
  the adapter did with it. That is where a 429 on each mode, a 401, an
  empty content array, and a thinking block are held.
- **Parse behavior** is pinned beside each extractor
  (`worker/tests/jobs/workflows/plan.test.ts` and its siblings): fenced
  output, prose around the JSON, wrong-typed fields, missing fields.

Separately, `tests/fixtures/llm/` is a recorded corpus keyed by the hash
of the request messages, replayed by a stub so the end-to-end flows
exercise real model output without a network call. A miss there means a
prompt stopped being deterministic: fix the prompt, never the key.

If a new model habit shows up, pin it first and then make the parser
cope, so "we think it copes" becomes recorded behavior.

---

## 6. Disclosure

When a deploy configures the shared tier, the settings page says so
plainly: prompts and card content sent through AI features leave this
deploy and are processed by a third-party inference service, and the
user's own key is the way to change that. There is no per-user disable
toggle; BYOK is the opt-out, and the disclosure exists so that choice
is informed.
