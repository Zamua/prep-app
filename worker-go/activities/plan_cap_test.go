package activities

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"prep-worker/agent"
	"prep-worker/shared"
)

// stubAgent returns canned stdout; enough to drive PlanCards without
// a real agent endpoint.
type stubAgent struct{ stdout string }

func (s stubAgent) Run(_ context.Context, _ agent.RunInput) (agent.RunOutput, error) {
	return agent.RunOutput{Stdout: s.stdout}, nil
}

func planJSON(n int) string {
	items := make([]string, 0, n)
	for i := range n {
		items = append(items, fmt.Sprintf(`{"title":"t%d","brief":"b","type":"short"}`, i))
	}
	return "[" + strings.Join(items, ",") + "]"
}

// MaxCards is enforced in code: an oversized plan is truncated, not trusted.
func TestPlanCardsEnforcesMaxCards(t *testing.T) {
	a := &Activities{Cfg: &Config{Agent: stubAgent{stdout: planJSON(8)}}}
	plan, err := a.PlanCards(context.Background(), shared.PlanCardsInput{
		UserID: "u", DeckName: "d", Prompt: "p", MaxCards: 5,
	})
	if err != nil {
		t.Fatalf("PlanCards: %v", err)
	}
	if len(plan) != 5 {
		t.Fatalf("want 5 cards, got %d", len(plan))
	}
}

// MaxCards == 0 leaves the plan uncapped.
func TestPlanCardsUncappedWhenMaxCardsZero(t *testing.T) {
	a := &Activities{Cfg: &Config{Agent: stubAgent{stdout: planJSON(8)}}}
	plan, err := a.PlanCards(context.Background(), shared.PlanCardsInput{
		UserID: "u", DeckName: "d", Prompt: "p",
	})
	if err != nil {
		t.Fatalf("PlanCards: %v", err)
	}
	if len(plan) != 8 {
		t.Fatalf("want 8 cards, got %d", len(plan))
	}
}

// MaxCards > 0 swaps the sizing paragraph for an explicit ceiling, on
// both the first-round and replan prompts.
func TestBuildPlanPromptStatesMaxCards(t *testing.T) {
	capped := buildPlanPrompt(shared.PlanCardsInput{DeckName: "d", Prompt: "topic", MaxCards: 5})
	if !strings.Contains(capped, "Create at most 5 cards") {
		t.Errorf("capped prompt missing ceiling: %q", capped)
	}
	if strings.Contains(capped, "Decide how many cards") {
		t.Errorf("capped prompt still lets the model pick the count")
	}

	uncapped := buildPlanPrompt(shared.PlanCardsInput{DeckName: "d", Prompt: "topic"})
	if !strings.Contains(uncapped, "Decide how many cards") {
		t.Errorf("uncapped prompt missing model-picks-count paragraph")
	}

	replan := buildPlanPrompt(shared.PlanCardsInput{
		DeckName:  "d",
		Prompt:    "topic",
		PriorPlan: []shared.PlanItem{{Title: "x", Brief: "y", Type: "short"}},
		Feedback:  "more",
		MaxCards:  5,
	})
	if !strings.Contains(replan, "at most 5 cards") {
		t.Errorf("replan prompt missing ceiling: %q", replan)
	}
}
