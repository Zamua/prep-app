// Package activities holds the side-effecting work the workflow orchestrates.
//
// All activities here are designed to be idempotent against repeat invocations
// triggered by Temporal's at-least-once delivery — we use either:
//   - a deterministic key (workflowID + index) checked in SQLite before the
//     side effect runs, or
//   - operations that are inherently idempotent (rm -f, "session jsonl
//     exists?" check before priming).
package activities

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"prep-worker/shared"
)

// emptyMCPConfig + strict-mcp-config tells claude to load NO MCP servers
// for this invocation. We use this on every worker shell-out so we don't
// race the channel-mode Claude's Telegram MCP for the bot's getUpdates slot.
//
// IMPORTANT: do NOT use --bare here. --bare disables OAuth/keychain reads
// too and breaks subscription auth ("Not logged in · Please run /login").
// strict-mcp-config keeps auth intact and only suppresses MCPs.
const emptyMCPConfig = `{"mcpServers":{}}`

// newSessionUUID returns a fresh random UUIDv4-shaped string. We mint one
// per PrimeClaudeSession attempt so retries after a failed prime never hit
// "Session ID X is already in use" (Claude registers the ID before fully
// creating the session, so collisions persist across retries).
func newSessionUUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant RFC 4122
	h := hex.EncodeToString(b)
	return fmt.Sprintf("%s-%s-%s-%s-%s", h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])
}

// Config holds host paths read from env at worker boot.
//
// AgentBin / AgentArgs configure the local agent CLI shell-out. Defaults
// match claude-code; users with a different harness (opencode, aider, …)
// can override via PREP_AGENT_BIN / PREP_AGENT_ARGS in main.go.
//
// CLAUDE_BIN is honored as a backward-compat alias for PREP_AGENT_BIN.
type Config struct {
	DBPath    string
	AgentBin  string
	AgentArgs string
}

func (c *Config) Validate() error {
	if c.DBPath == "" {
		// Mirror the Python side's default: data.sqlite next to the
		// running binary's working dir. For `make dev` this is the repo
		// root; for prod it's the prep-app/ checkout dir. Both contain a
		// data.sqlite that the FastAPI process writes to.
		c.DBPath = "./data.sqlite"
	}
	if c.AgentBin == "" {
		return errors.New("PREP_AGENT_BIN (or CLAUDE_BIN) unset — set to the local agent CLI binary")
	}
	return nil
}

// agentArgs renders the configured arg template, substituting placeholders
// and appending the prompt as the last arg. Defaults match claude-code's
// argument shape.
const defaultAgentArgs = "--strict-mcp-config,--mcp-config,{mcp_config},-p"

func (c *Config) agentArgs(prompt string) []string {
	csv := c.AgentArgs
	if csv == "" {
		csv = defaultAgentArgs
	}
	out := []string{}
	for _, a := range strings.Split(csv, ",") {
		a = strings.TrimSpace(a)
		if a == "" {
			continue
		}
		a = strings.ReplaceAll(a, "{mcp_config}", emptyMCPConfig)
		out = append(out, a)
	}
	return append(out, prompt)
}

// Activities groups all activity methods so we can register them under one
// receiver and share Config.
type Activities struct {
	Cfg *Config
}

// ---- Deck context loading ----------------------------------------------

// DeckContext is bundled into the priming prompt — the user-supplied focus
// (from decks.context_prompt, set via the new-deck UI) plus every existing
// prompt for the deck (for cross-batch dedup).
type DeckContext struct {
	deckName        string
	focus           string
	existingPrompts []string
}

func (a *Activities) loadDeckContext(userID, deckName string) (*DeckContext, error) {
	prior, err := allPromptsForDeck(a.Cfg.DBPath, userID, deckName)
	if err != nil {
		return nil, fmt.Errorf("read prior prompts: %w", err)
	}
	dbPrompt, err := getDeckContextPrompt(a.Cfg.DBPath, userID, deckName)
	if err != nil {
		return nil, fmt.Errorf("read context_prompt: %w", err)
	}
	focus := strings.TrimSpace(dbPrompt)
	if focus == "" {
		return nil, fmt.Errorf("deck %q has no context_prompt — set one via the UI first", deckName)
	}
	return &DeckContext{
		deckName:        deckName,
		focus:           focus,
		existingPrompts: prior,
	}, nil
}

// ---- Activity: PrimeClaudeSession --------------------------------------

// PrimeClaudeSession mints a fresh session ID, then seeds a claude session
// with all the deck context up front so subsequent GenerateNextCard calls
// only have to add a one-line "now generate card #i" prompt. Anthropic's
// prompt cache then makes the per-card calls cheap.
//
// Returns the session ID it created. The workflow stores this and passes
// it to all GenerateNextCard / Cleanup activities.
//
// Retry semantics: each call mints a fresh UUID, so retries after a partial
// failure never collide on "Session ID X is already in use." Temporal's
// at-least-once delivery means a successful call's ID gets recorded in
// workflow history; subsequent re-deliveries of the same activity result
// reuse that ID via history replay.
func (a *Activities) PrimeClaudeSession(ctx context.Context, in shared.PrimeInput) (shared.PrimeResult, error) {
	logger := activity.GetLogger(ctx)
	sessionID := newSessionUUID()

	dctx, err := a.loadDeckContext(in.UserID, in.DeckName)
	if err != nil {
		return shared.PrimeResult{}, temporal.NewNonRetryableApplicationError(
			"deck context load failed", "BadDeckContext", err)
	}

	primePrompt := fmt.Sprintf(`You are about to generate flashcard questions for an interview-prep app, one card at a time, over multiple turns. This first message gives you ALL the context. Acknowledge in one short sentence — do not generate any cards yet.

**Deck:** %s

**Focus / context (provided by the user):**
%s

If the description above contains URLs or references recent material, you may use your web-fetch / web-search tools to ground the questions in current information.

**Existing question prompts in this deck (do NOT duplicate or paraphrase any of these in subsequent cards):**
%s

---

Acknowledge: "Ready to generate cards for %s." Nothing else.`,
		dctx.deckName,
		dctx.focus,
		joinPrompts(dctx.existingPrompts),
		dctx.deckName,
	)

	// --session-id is the one extra arg PrimeClaudeSession passes that the
	// agent helper doesn't (it's about session persistence for prompt-cache
	// reuse, specific to claude). We splice it in at the front of the
	// configured agent args. For non-claude agents that don't support
	// --session-id, the user's PREP_AGENT_ARGS would need to include
	// equivalent flags or this codepath needs adjustment — claude is the
	// documented v1 agent.
	args := append([]string{"--session-id", sessionID}, a.Cfg.agentArgs(primePrompt)...)
	cmd := exec.CommandContext(ctx, a.Cfg.AgentBin, args...)
	cmd.Env = os.Environ()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return shared.PrimeResult{}, fmt.Errorf("claude prime failed: %w (output: %s)", err, truncate(string(out), 800))
	}
	logger.Info("primed", "session_id", sessionID, "ack", truncate(string(out), 200))
	return shared.PrimeResult{SessionID: sessionID}, nil
}

// ---- Activity: GenerateNextCard ----------------------------------------

// GenerateNextCard resumes the primed session and asks for one card.
// Idempotent: looks up `idempotency_key` in the questions table first;
// if found, returns the existing card without calling claude.
func (a *Activities) GenerateNextCard(ctx context.Context, in shared.GenerateInput) (shared.Card, error) {
	logger := activity.GetLogger(ctx)

	// Idempotency check.
	if existing, found, err := getCardByIdempotencyKey(a.Cfg.DBPath, in.IdempotencyKey); err != nil {
		return shared.Card{}, fmt.Errorf("idempotency lookup: %w", err)
	} else if found {
		logger.Info("card already generated for this key, returning cached",
			"idempotency_key", in.IdempotencyKey)
		return existing, nil
	}

	dedup := strings.Builder{}
	for _, p := range in.PriorPrompts {
		fmt.Fprintf(&dedup, "- %s\n", truncate(p, 180))
	}

	prompt := fmt.Sprintf(`Generate ONE flashcard question. This is card %d of %d.

Avoid duplicating these prompts you just generated:
%s
Output a single JSON object (no prose, no fences). Required fields:
- "type": one of "code" | "mcq" | "multi" | "short"
- "topic": short string tag
- "prompt": markdown ok
- "choices": array (REQUIRED for mcq/multi, OMIT otherwise)
- "answer": string (for multi: a JSON-encoded array of the correct choices)
- "rubric": 2-4 short bullet lines describing what a correct answer must demonstrate
- "skeleton": OPTIONAL. For "code" questions only. Include MINIMAL starter code
  ONLY when the canonical version of the problem provides scaffolding the user
  fills in — e.g. LeetCode 1114/1115/1116/1117/1195/1226 (concurrency series),
  problems where a class signature with empty methods is the natural fixture.
  OMIT for problems where designing the structure (data class, type, function
  signature) is itself part of the test. When present, keep it minimal: type
  declaration + method stubs with EMPTY bodies, NOT a partial implementation.
  CRITICAL: do NOT include placeholder/explanatory comments inside method
  bodies — no slash-star ellipsis comments, no // TODO, no // your code here,
  no // fill in. Leave bodies empty (curly braces with no content, or open
  brace on one line and close brace on the next). The prompt itself explains
  what to do; the skeleton should only carry structure, not narration.
- "language": REQUIRED for "code" questions; one of "go" | "java" | "python" |
  "javascript" | "typescript" | "rust" | "cpp". The language the user is meant
  to write the answer in. Match the language to whatever the prompt asks for
  (e.g. "Implement X **in Go**" → "go"). Drives the editor's syntax highlighting.
  OMIT for non-code questions.

Output ONLY the JSON object.`,
		in.Index, in.Total, dedup.String())

	// Heartbeat loop while claude runs — keeps Temporal from declaring the
	// activity stuck if claude takes 30+s.
	done := make(chan struct{})
	defer close(done)
	go func() {
		t := time.NewTicker(10 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-done:
				return
			case <-t.C:
				activity.RecordHeartbeat(ctx, fmt.Sprintf("waiting on claude (card %d/%d)", in.Index, in.Total))
			}
		}
	}()

	// `claude --resume <id>` resumes by session ID. NOT `--session-id X --resume`
	// — that combo errors with "--session-id can only be used with --continue or
	// --resume if --fork-session is also specified" (we DON'T want fork because
	// fork creates a new session ID).
	//
	// Same agent helper, with --resume <id> spliced in to reuse the primed
	// session for prompt-cache wins.
	args := append([]string{"--resume", in.SessionID}, a.Cfg.agentArgs(prompt)...)
	cmd := exec.CommandContext(ctx, a.Cfg.AgentBin, args...)
	cmd.Env = os.Environ()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return shared.Card{}, fmt.Errorf("claude resume failed: %w (output: %s)", err, truncate(string(out), 800))
	}

	card, err := parseCardJSON(out)
	if err != nil {
		// Bad JSON from the model is a terminal failure for this card —
		// don't retry, the workflow can decide to skip and continue.
		return shared.Card{}, temporal.NewNonRetryableApplicationError(
			"card JSON parse failed", "BadCardJSON",
			fmt.Errorf("%w: %s", err, truncate(string(out), 800)))
	}
	return card, nil
}

// ---- Activity: InsertCard ----------------------------------------------

// InsertCard writes a card to the prep-app's SQLite. Idempotent via the
// `idempotency_key` UNIQUE constraint added to the questions table.
func (a *Activities) InsertCard(ctx context.Context, in shared.InsertInput) (shared.InsertResult, error) {
	return insertCard(a.Cfg.DBPath, in)
}

// ---- Activity: Cleanup -------------------------------------------------

// Cleanup deletes the claude session jsonl after the workflow is done.
// Inherently idempotent — `rm -f` on a missing file is a no-op.
func (a *Activities) Cleanup(ctx context.Context, in shared.CleanupInput) error {
	paths := claudeSessionPaths(in.SessionID)
	for _, p := range paths {
		_ = os.Remove(p) // best-effort
	}
	return nil
}

// ---- Helpers (small enough to stay here) -------------------------------

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

func parseCardJSON(out []byte) (shared.Card, error) {
	raw := strings.TrimSpace(string(out))
	// Strip optional markdown fences.
	raw = strings.TrimPrefix(raw, "```json")
	raw = strings.TrimPrefix(raw, "```")
	raw = strings.TrimSuffix(raw, "```")
	raw = strings.TrimSpace(raw)
	// Trim anything before { and after }.
	if i := strings.Index(raw, "{"); i > 0 {
		raw = raw[i:]
	}
	if i := strings.LastIndex(raw, "}"); i >= 0 && i < len(raw)-1 {
		raw = raw[:i+1]
	}

	var card shared.Card
	// First try the canonical decode.
	if err := json.Unmarshal([]byte(raw), &card); err == nil {
		if card.Type == "" || card.Prompt == "" || card.Answer == "" {
			return shared.Card{}, errors.New("missing required fields (type/prompt/answer)")
		}
		return card, nil
	}
	// Some models wrap the answer field as a list — fall through with a
	// permissive decode and re-coerce.
	var loose map[string]any
	if err := json.Unmarshal([]byte(raw), &loose); err != nil {
		return shared.Card{}, fmt.Errorf("not JSON: %w", err)
	}
	card.Type, _ = loose["type"].(string)
	card.Topic, _ = loose["topic"].(string)
	card.Prompt, _ = loose["prompt"].(string)
	card.Rubric = coerceRubric(loose["rubric"])
	card.Answer = coerceAnswer(loose["answer"])
	card.Skeleton, _ = loose["skeleton"].(string)
	card.Language, _ = loose["language"].(string)
	if c, ok := loose["choices"].([]any); ok {
		for _, x := range c {
			if s, ok := x.(string); ok {
				card.Choices = append(card.Choices, s)
			}
		}
	}
	if card.Type == "" || card.Prompt == "" || card.Answer == "" {
		return shared.Card{}, errors.New("missing required fields after coercion")
	}
	return card, nil
}

func coerceRubric(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case []any:
		var b strings.Builder
		for _, item := range x {
			if s, ok := item.(string); ok {
				fmt.Fprintf(&b, "- %s\n", s)
			}
		}
		return strings.TrimRight(b.String(), "\n")
	}
	return ""
}

func coerceAnswer(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case []any:
		// `multi` answer as a list — JSON-encode for storage.
		if data, err := json.Marshal(x); err == nil {
			return string(data)
		}
	}
	return ""
}

func joinPrompts(prompts []string) string {
	if len(prompts) == 0 {
		return "(none yet)"
	}
	var b strings.Builder
	for _, p := range prompts {
		fmt.Fprintf(&b, "- %s\n", truncate(p, 200))
	}
	return strings.TrimRight(b.String(), "\n")
}

