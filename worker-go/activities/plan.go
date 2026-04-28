// Plan-first generation activities. Two activities:
//
//   PlanCards            — claude returns a brief outline (titles + briefs).
//                          Used by the workflow to present a reviewable plan
//                          to the user before any full-card generation.
//   GenerateCardFromBrief — given one PlanItem + the deck's standing
//                          description, claude expands it into a full Card.
//                          Called in parallel (one per item) on accept.
//
// These replace the previous Prime/Resume session-based approach for the
// "create new deck" flow. We trade the prompt-cache wins of one shared
// session for a much cleaner UX (interactive plan + parallel execution).
// The Transform workflow still uses the older shape; it has a different
// shape (rewrite existing cards, not create from scratch).

package activities

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"prep-worker/agent"
	"prep-worker/shared"
)

// PlanCards asks claude for a brief outline of cards. Cheap call: claude
// only returns titles + 1-2 sentence briefs, not full content. PriorPlan
// + Feedback are used on replan rounds to nudge the outline.
func (a *Activities) PlanCards(ctx context.Context, in shared.PlanCardsInput) ([]shared.PlanItem, error) {
	if a.Cfg.Agent == nil {
		return nil, noAgentErr("PlanCards")
	}

	// Heartbeat — claude planning runs are typically a few seconds but a
	// stuck call shouldn't pin a worker.
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
				activity.RecordHeartbeat(ctx, "planning")
			}
		}
	}()

	prompt := buildPlanPrompt(in)
	out, err := a.Cfg.Agent.Run(ctx, agent.RunInput{Prompt: prompt})
	if err != nil {
		return nil, fmt.Errorf("agent plan failed: %w", err)
	}

	plan, err := parsePlanJSON([]byte(out.Stdout))
	if err != nil {
		// Bad JSON is non-retryable — the workflow surfaces it to the
		// user, who can either retry by sending feedback or reject.
		return nil, temporal.NewNonRetryableApplicationError(
			"plan JSON parse failed", "BadPlanJSON",
			fmt.Errorf("%w: %s", err, truncate(out.Stdout, 800)))
	}
	return plan, nil
}

func buildPlanPrompt(in shared.PlanCardsInput) string {
	var b strings.Builder

	if len(in.PriorPlan) == 0 {
		// First-round prompt.
		b.WriteString(fmt.Sprintf(
			`You are planning a set of interview-prep flashcards for the deck "%s".

The user provided this description / topic:

%s

Decide how many cards to create — let the description guide you. Most
decks want 5-15 cards covering the main concepts; a tightly-scoped
description might warrant only 3, a broad survey might warrant 20+.
Don't pad. Don't skimp.

Return a JSON array of cards. Each entry is an OBJECT with these fields:

  - "title":    a short label (3-8 words). What the card is about.
  - "brief":    1-2 sentences describing what the question will ask.
  - "type":     one of "code" | "mcq" | "multi" | "short". Pick the type
                that matches the brief best — code for "implement X",
                mcq/multi for fact recall, short for "explain Y".
  - "topic":    optional short tag for grouping (e.g. "concurrency",
                "system design", "behavioral").
  - "language": REQUIRED only when type=="code"; one of
                go|java|python|javascript|typescript|rust|cpp.

Output ONLY the JSON array, no prose, no fences.`,
			in.DeckName, in.Prompt))
	} else {
		// Replan with the user's feedback. Show the prior plan so claude
		// can amend rather than start over.
		b.WriteString(fmt.Sprintf(
			`Refine the card plan for deck "%s".

Original description:
%s

Your previous plan (%d cards):
%s

The user wants this changed:
%s

Output a NEW JSON array (same field shape: title, brief, type, topic,
language?). Apply the user's feedback. You may add, remove, replace,
or reorder items. Output ONLY the JSON array.`,
			in.DeckName, in.Prompt, len(in.PriorPlan),
			renderPriorPlan(in.PriorPlan), in.Feedback))
	}

	return b.String()
}

func renderPriorPlan(items []shared.PlanItem) string {
	var b strings.Builder
	for i, it := range items {
		fmt.Fprintf(&b, "%d. [%s] %s — %s\n", i+1, it.Type, it.Title, truncate(it.Brief, 200))
	}
	return b.String()
}

// parsePlanJSON tolerates a couple of common shapes claude might return:
//
//   - the raw array literal (preferred, what the prompt asks for)
//   - a wrapper object {"plan": [...]} (claude sometimes hedges)
//   - a fenced ```json block (despite our "no fences" instruction)
func parsePlanJSON(raw []byte) ([]shared.PlanItem, error) {
	body := unfence(strings.TrimSpace(string(raw)))

	// Try array first.
	var arr []shared.PlanItem
	if err := json.Unmarshal([]byte(body), &arr); err == nil {
		if len(arr) == 0 {
			return nil, fmt.Errorf("plan is empty")
		}
		for i := range arr {
			arr[i].Title = strings.TrimSpace(arr[i].Title)
			arr[i].Brief = strings.TrimSpace(arr[i].Brief)
			arr[i].Type = strings.TrimSpace(arr[i].Type)
		}
		return arr, nil
	}

	// Try object wrapper.
	var wrap struct {
		Plan []shared.PlanItem `json:"plan"`
	}
	if err := json.Unmarshal([]byte(body), &wrap); err == nil && len(wrap.Plan) > 0 {
		return wrap.Plan, nil
	}

	return nil, fmt.Errorf("could not parse plan JSON: %s", truncate(body, 400))
}

func unfence(s string) string {
	// Strip leading/trailing ``` or ```json fences if present.
	if strings.HasPrefix(s, "```") {
		// Find first newline, drop everything up to and including it.
		if i := strings.Index(s, "\n"); i >= 0 {
			s = s[i+1:]
		}
		if j := strings.LastIndex(s, "```"); j >= 0 {
			s = s[:j]
		}
	}
	return strings.TrimSpace(s)
}

// GenerateCardFromBrief expands one PlanItem into a full Card via claude.
// Designed to run in parallel with siblings; each call is independent and
// has its own claude session (no shared priming — the per-call prompt
// carries the deck description inline).
func (a *Activities) GenerateCardFromBrief(ctx context.Context, in shared.GenerateCardFromBriefInput) (shared.Card, error) {
	if a.Cfg.Agent == nil {
		return shared.Card{}, noAgentErr("GenerateCardFromBrief")
	}

	// Idempotency check first — workflow may re-invoke after a transient
	// failure; we don't want to pay for the same card twice.
	if existing, found, err := getCardByIdempotencyKey(a.Cfg.DBPath, in.IdempotencyKey); err != nil {
		return shared.Card{}, fmt.Errorf("idempotency lookup: %w", err)
	} else if found {
		return existing, nil
	}

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
				activity.RecordHeartbeat(ctx, fmt.Sprintf("expanding %d/%d", in.Index+1, in.Total))
			}
		}
	}()

	prompt := fmt.Sprintf(`Generate ONE flashcard for deck "%s".

Deck description (for grounding):
%s

This card was planned as:
  title: %s
  brief: %s
  type:  %s%s

Write the FULL card content matching the planned title + brief + type.
Output a single JSON object (no prose, no fences). Required fields:

  - "type":     "%s"  (use the planned type)
  - "topic":    short string tag
  - "prompt":   the question text. markdown ok.
  - "choices":  array, REQUIRED for mcq/multi, OMIT otherwise.
  - "answer":   string. for multi: a JSON-encoded array of correct choices.
  - "rubric":   2-4 short bullet lines describing what a correct answer
                must demonstrate.
  - "skeleton": OPTIONAL. for code questions only. minimal starter
                scaffold (class signature with empty method bodies) when
                the canonical version of the problem provides one. OMIT
                otherwise. NEVER include placeholder comments inside
                method bodies.
  - "language": REQUIRED for code. one of go|java|python|javascript|
                typescript|rust|cpp. Match the brief.

Output ONLY the JSON object.`,
		in.DeckName, in.DeckPrompt,
		in.Item.Title, in.Item.Brief, in.Item.Type,
		optLanguageHint(in.Item),
		in.Item.Type)

	out, err := a.Cfg.Agent.Run(ctx, agent.RunInput{Prompt: prompt})
	if err != nil {
		return shared.Card{}, fmt.Errorf("agent expand failed: %w", err)
	}

	card, err := parseCardJSON([]byte(out.Stdout))
	if err != nil {
		return shared.Card{}, temporal.NewNonRetryableApplicationError(
			"card JSON parse failed", "BadCardJSON",
			fmt.Errorf("%w: %s", err, truncate(out.Stdout, 800)))
	}
	// Backfill fields from the plan if claude omitted them.
	if card.Topic == "" && in.Item.Topic != "" {
		card.Topic = in.Item.Topic
	}
	return card, nil
}

func optLanguageHint(p shared.PlanItem) string {
	if p.Type == "code" && p.Language != "" {
		return fmt.Sprintf("\n  language: %s", p.Language)
	}
	return ""
}
