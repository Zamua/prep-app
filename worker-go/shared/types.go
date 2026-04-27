// Package shared holds the input/output types used across the workflow
// and activities. Keeping them in one place makes the contract obvious
// and prevents accidental drift.
package shared

const (
	TaskQueue        = "prep-generation"
	WorkflowGenerate = "GenerateCardsWorkflow"
	WorkflowGrade    = "GradeAnswerWorkflow"
)

// GenerateCardsInput is the workflow's input — what the FastAPI app
// hands us when the user clicks Generate.
type GenerateCardsInput struct {
	DeckName string `json:"deck_name"`
	Count    int    `json:"count"`
	UserID   string `json:"user_id"` // tailscale_login of the user who owns this deck
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
	UserID   string `json:"user_id"`
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
	UserID         string   `json:"user_id"`
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
	// Skeleton is optional starter code that prefills the user's answer
	// textarea. Only meaningful for `code` questions and only when the
	// canonical version of the problem is "fill in the blanks" (LeetCode
	// concurrency series, threading primitives where the class signature
	// is given). Generators should leave it empty for problems where
	// reproducing the structure is part of the test.
	Skeleton string `json:"skeleton,omitempty"`
	// Language is the CodeMirror lang id used to highlight the editor:
	// "go" | "java" | "python" | "javascript" | "typescript" | "rust" | "cpp".
	// Only meaningful for `code` questions; ignored otherwise.
	Language string `json:"language,omitempty"`
}

// InsertInput is the input for InsertCard.
type InsertInput struct {
	DeckName       string `json:"deck_name"`
	UserID         string `json:"user_id"`
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

// ---- Grading workflow types ----

// GradeAnswerInput is the input to GradeAnswerWorkflow.
type GradeAnswerInput struct {
	QuestionID int    `json:"question_id"`
	UserAnswer string `json:"user_answer"`
	IDK        bool   `json:"idk"`
	UserID     string `json:"user_id"`
}

// Verdict is what GradeFreeText returns and what the workflow ultimately
// surfaces. Mirrors the shape grader.py:_grade_freetext returns.
type Verdict struct {
	Result              string `json:"result"` // "right" | "wrong"
	Feedback            string `json:"feedback"`
	ModelAnswerSummary  string `json:"model_answer_summary"`
}

// GradeFreeTextInput is the activity input. Question is fetched inside the
// activity to keep the workflow's input small (Temporal payloads have a
// practical size limit; passing the whole question payload is wasteful).
type GradeFreeTextInput struct {
	QuestionID int    `json:"question_id"`
	UserAnswer string `json:"user_answer"`
	IDK        bool   `json:"idk"`
	UserID     string `json:"user_id"`
}

// RecordReviewInput is the activity input for writing the review row +
// advancing the SRS state.
type RecordReviewInput struct {
	QuestionID     int    `json:"question_id"`
	UserID         string `json:"user_id"`
	Result         string `json:"result"`
	UserAnswer     string `json:"user_answer"`
	GraderNotes    string `json:"grader_notes"`
	IdempotencyKey string `json:"idempotency_key"` // = workflow_id
}

// SRSState mirrors what db.py:record_review returns — used by the polling
// page to render "next due in X min" + step.
type SRSState struct {
	Step            int    `json:"step"`
	NextDue         string `json:"next_due"`
	IntervalMinutes int    `json:"interval_minutes"`
}

// GradeAnswerResult is the workflow output, also exposed via the
// getGradeProgress query for live status.
type GradeAnswerResult struct {
	QuestionID int      `json:"question_id"`
	UserAnswer string   `json:"user_answer"`
	IDK        bool     `json:"idk"`
	Verdict    Verdict  `json:"verdict"`
	State      SRSState `json:"state"`
}

// GradeProgress is the shape returned by the getGradeProgress query.
type GradeProgress struct {
	Status     string             `json:"status"` // "grading" | "recording" | "done" | "failed"
	StartedAt  string             `json:"started_at"`
	FinishedAt string             `json:"finished_at,omitempty"`
	Result     *GradeAnswerResult `json:"result,omitempty"` // populated when status=done
}
