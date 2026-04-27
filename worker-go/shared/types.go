// Package shared holds the input/output types used across the workflow
// and activities. Keeping them in one place makes the contract obvious
// and prevents accidental drift.
package shared

const (
	TaskQueue       = "prep-generation"
	WorkflowGenerate = "GenerateCardsWorkflow"
)

// GenerateCardsInput is the workflow's input — what the FastAPI app
// hands us when the user clicks Generate.
type GenerateCardsInput struct {
	DeckName string `json:"deck_name"`
	Count    int    `json:"count"`
}

// GenerateCardsResult is what the workflow returns when it completes.
type GenerateCardsResult struct {
	DeckName string `json:"deck_name"`
	Inserted []int  `json:"inserted_card_ids"`
}

// Progress is the shape returned by the getProgress query handler.
// FastAPI polls this to render the status page.
type Progress struct {
	Total           int    `json:"total"`
	Completed       int    `json:"completed"`
	CurrentTopic    string `json:"current_topic,omitempty"`
	StartedAt       string `json:"started_at"`
	LastCardAt      string `json:"last_card_at,omitempty"`
	Status          string `json:"status"` // "priming" | "generating" | "done" | "cancelling" | "failed"
}

// PrimeInput is the input for PrimeClaudeSession.
//
// Note: the session ID is generated INSIDE the activity and returned via
// PrimeResult, NOT passed in. This avoids the "Session ID X is already in
// use" error when an attempt fails partway through (Claude registers the
// ID before fully creating the session). Each retry mints a fresh ID.
type PrimeInput struct {
	DeckName string `json:"deck_name"`
}

// PrimeResult is what PrimeClaudeSession returns. The workflow stores
// SessionID and passes it to all subsequent GenerateNextCard calls.
type PrimeResult struct {
	SessionID string `json:"session_id"`
}

// GenerateInput is the input for GenerateNextCard. The IdempotencyKey is
// `<workflowID>-<index>` and is what we use to dedupe on retry.
type GenerateInput struct {
	SessionID      string   `json:"session_id"`
	DeckName       string   `json:"deck_name"`
	Index          int      `json:"index"`
	Total          int      `json:"total"`
	IdempotencyKey string   `json:"idempotency_key"`
	PriorPrompts   []string `json:"prior_prompts"` // last N for in-batch dedup
}

// Card mirrors the JSON payload Claude returns and the prep-app's
// questions schema.
type Card struct {
	Type    string   `json:"type"`    // code | mcq | multi | short
	Topic   string   `json:"topic"`
	Prompt  string   `json:"prompt"`
	Choices []string `json:"choices,omitempty"`
	Answer  string   `json:"answer"`
	Rubric  string   `json:"rubric"`
}

// InsertInput is the input for InsertCard.
type InsertInput struct {
	DeckName       string `json:"deck_name"`
	IdempotencyKey string `json:"idempotency_key"`
	Card           Card   `json:"card"`
}

// InsertResult is what InsertCard returns.
type InsertResult struct {
	CardID    int  `json:"card_id"`
	Duplicate bool `json:"duplicate"` // true if INSERT was a no-op (already existed)
}

// CleanupInput cleans up the on-disk Claude session jsonl after a workflow
// finishes. Idempotent (rm -f).
type CleanupInput struct {
	SessionID string `json:"session_id"`
}

// NotifyInput is the input for NotifyTelegram.
type NotifyInput struct {
	Text string `json:"text"`
}
