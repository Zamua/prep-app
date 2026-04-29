// Package activities holds the side-effecting work the workflows
// orchestrate. Each activity is idempotent against repeat invocations
// triggered by Temporal's at-least-once delivery — either via a
// deterministic key (workflowID + index) checked before the side
// effect, or via inherently idempotent ops.
package activities

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"go.temporal.io/sdk/temporal"

	"prep-worker/agent"
	"prep-worker/shared"
)

// Config holds host paths + the agent client read from env at worker
// boot. The Agent field is the single seam through which all AI calls
// flow (see prep-worker/agent). Activities go through Agent.Run rather
// than touching exec.Command or http.Client directly.
//
// Agent may be nil if no agent is configured at boot (PREP_AGENT_URL
// and PREP_AGENT_BIN both unset). Activities that need the agent
// return a non-retryable NoAgent error so workflows surface it to the
// user instead of looping retries.
type Config struct {
	DBPath string
	Agent  agent.Client
}

func (c *Config) Validate() error {
	if c.DBPath == "" {
		// Default mirrors the Python side's: data.sqlite next to the
		// running binary's working dir. Inside docker the container's
		// PREP_DB_PATH points at the mounted volume.
		c.DBPath = "./data.sqlite"
	}
	// Agent allowed to be nil — manual-only mode is supported. Per-
	// activity callers gate on c.Agent before dispatching AI work.
	return nil
}

// noAgentErr is returned to the workflow when an AI-needing activity
// is invoked without an agent configured. Non-retryable so the user
// sees a real error instead of "still trying."
func noAgentErr(activity string) error {
	return temporal.NewNonRetryableApplicationError(
		fmt.Sprintf("%s needs an AI agent but none is configured (PREP_AGENT_URL / PREP_AGENT_BIN unset)", activity),
		"NoAgent", nil)
}

// Activities groups all activity methods so we can register them under
// one receiver and share Config.
type Activities struct {
	Cfg *Config
}

// ---- Activity: InsertCard ----------------------------------------------

// InsertCard writes a card to the prep-app's SQLite. Idempotent via the
// `idempotency_key` UNIQUE constraint on the questions table.
func (a *Activities) InsertCard(ctx context.Context, in shared.InsertInput) (shared.InsertResult, error) {
	return insertCard(a.Cfg.DBPath, in)
}

// ---- Helpers shared across activities ----------------------------------

// parseCardJSON is the canonical "claude returned a Card-shaped JSON,
// parse it tolerantly" helper — used by plan.go (per-card expansion)
// and transform.go (per-card additions). truncate is logging shorthand
// used everywhere.

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// parseCardJSON parses a Card from claude's stdout. Tolerates fenced
// code blocks, leading/trailing prose, and "answer-as-list" shapes
// (multi questions sometimes come back that way).
func parseCardJSON(out []byte) (shared.Card, error) {
	raw := strings.TrimSpace(string(out))
	raw = strings.TrimPrefix(raw, "```json")
	raw = strings.TrimPrefix(raw, "```")
	raw = strings.TrimSuffix(raw, "```")
	raw = strings.TrimSpace(raw)
	if i := strings.Index(raw, "{"); i > 0 {
		raw = raw[i:]
	}
	if i := strings.LastIndex(raw, "}"); i >= 0 && i < len(raw)-1 {
		raw = raw[:i+1]
	}

	var card shared.Card
	if err := json.Unmarshal([]byte(raw), &card); err == nil {
		if card.Type == "" || card.Prompt == "" || card.Answer == "" {
			return shared.Card{}, errors.New("missing required fields (type/prompt/answer)")
		}
		return card, nil
	}
	// Permissive decode for cards that wrap fields in unexpected types.
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
