// Package shared holds the input/output types used across the workflow
// and activities. Keeping them in one place makes the contract obvious
// and prevents accidental drift.
package shared

const (
	TaskQueue              = "prep-generation"
	WorkflowGrade          = "GradeAnswerWorkflow"
	WorkflowTransform      = "TransformWorkflow"
	WorkflowPlanGenerate   = "PlanGenerateWorkflow"
	WorkflowTriviaGenerate = "TriviaGenerateWorkflow"

	// Signals + queries on TransformWorkflow.
	SignalApplyTransform   = "applyTransform"
	SignalRejectTransform  = "rejectTransform"
	QueryTransformProgress = "getTransformProgress"

	// Signals + queries on PlanGenerateWorkflow.
	SignalPlanFeedback = "planFeedback"
	SignalPlanAccept   = "planAccept"
	SignalPlanReject   = "planReject"
	QueryPlanProgress  = "getPlanProgress"

	// Signals + queries on TriviaGenerateWorkflow.
	SignalTriviaFeedback = "triviaFeedback"
	SignalTriviaAccept   = "triviaAccept"
	SignalTriviaReject   = "triviaReject"
	QueryTriviaProgress  = "getTriviaProgress"
)

// ---- TriviaGenerate (notification-driven decks) -------------------------
//
// Single-claude-call workflow: ask the agent for N short-Q-short-A pairs,
// dedupe against the deck's existing prompts, insert each via a tiny
// per-pair activity (so a transient db blip doesn't lose the whole batch),
// expose progress via a query handler so the UI can poll.

type TriviaGenerateInput struct {
	UserID    string `json:"user_id"`
	DeckID    int    `json:"deck_id"`
	DeckName  string `json:"deck_name"`
	Topic     string `json:"topic"`      // free-text user prompt; claude reads it
	BatchSize int    `json:"batch_size"` // 0 → use default (25)
}

// TriviaGenerateProgress is the live state queried by the polling
// page. Status walks through:
//
//	planning → awaiting_feedback → (replanning → awaiting_feedback)*
//	  → generating → applying → done
//
// Or terminates early at `rejected` (user dismissed plan) or `failed`
// (claude error / no cards expanded). Plan + Round let the UI render
// the proposed list and number of replans; GeneratedCount/Total drive
// the per-card progress bar in the expansion phase.
type TriviaGenerateProgress struct {
	Status         string           `json:"status"`
	Plan           []TriviaPlanItem `json:"plan,omitempty"`
	Round          int              `json:"round"`
	Total          int              `json:"total"`
	GeneratedCount int              `json:"generated_count"`
	Inserted       int              `json:"inserted"`
	SkippedDups    int              `json:"skipped_dups"`
	SkippedInvalid int              `json:"skipped_invalid"`
	StartedAt      string           `json:"started_at"`
	FinishedAt     string           `json:"finished_at,omitempty"`
	Error          string           `json:"error,omitempty"`
}

type TriviaGenerateResult struct {
	Status         string `json:"status"` // "completed" | "rejected"
	Inserted       int    `json:"inserted"`
	SkippedDups    int    `json:"skipped_dups"`
	SkippedInvalid int    `json:"skipped_invalid"`
}

// TriviaPair is one Q/A from claude. Mirrors the JSON the agent returns.
// `E` is the deeper explanation revealed in the card view's "Deep dive"
// disclosure — optional so legacy claude responses without it still parse.
type TriviaPair struct {
	Q string `json:"q"`
	A string `json:"a"`
	E string `json:"e,omitempty"`
}

// TriviaPlanItem is one entry in claude's planning outline: a short
// title + 1-2 sentence brief describing what the card will ask. The
// expansion phase turns each item into a full TriviaPair (q+a+e).
type TriviaPlanItem struct {
	Title string `json:"title"`
	Brief string `json:"brief"`
}

// GenerateTriviaInput is the legacy single-shot batch input. Kept
// for backwards-compat with workflows still in flight; new code
// uses PlanTriviaBatchInput + GenerateTriviaCardFromBriefInput.
type GenerateTriviaInput struct {
	UserID    string   `json:"user_id"`
	DeckID    int      `json:"deck_id"`
	Topic     string   `json:"topic"`
	Existing  []string `json:"existing"`
	BatchSize int      `json:"batch_size"`
}

// PlanTriviaBatchInput drives the planning activity. The first round
// has no PriorPlan/Feedback; subsequent rounds carry both so claude
// can incorporate the user's redirect.
type PlanTriviaBatchInput struct {
	UserID    string           `json:"user_id"`
	DeckID    int              `json:"deck_id"`
	Topic     string           `json:"topic"`
	BatchSize int              `json:"batch_size"`
	Existing  []string         `json:"existing"`
	PriorPlan []TriviaPlanItem `json:"prior_plan,omitempty"`
	Feedback  string           `json:"feedback,omitempty"`
}

// GenerateTriviaCardFromBriefInput drives one parallel expansion. The
// idempotency key lets the activity skip claude on retry if a previous
// attempt already produced the pair (kept on disk by question_id +
// queue_position once InsertTriviaCard runs, but the expansion step
// itself doesn't write — so the key is workflow-id + index, matching
// the pattern PlanGenerate uses for SRS cards).
type GenerateTriviaCardFromBriefInput struct {
	UserID         string         `json:"user_id"`
	DeckID         int            `json:"deck_id"`
	DeckName       string         `json:"deck_name"`
	Topic          string         `json:"topic"`
	Item           TriviaPlanItem `json:"item"`
	Index          int            `json:"index"`
	Total          int            `json:"total"`
	IdempotencyKey string         `json:"idempotency_key"`
}

// InsertTriviaCardInput drives the per-card insert activity.
type InsertTriviaCardInput struct {
	UserID      string `json:"user_id"`
	DeckID      int    `json:"deck_id"`
	Topic       string `json:"topic"`
	Prompt      string `json:"prompt"`
	Answer      string `json:"answer"`
	Explanation string `json:"explanation,omitempty"`
}

type InsertTriviaCardResult struct {
	QuestionID    int  `json:"question_id"`
	Duplicate     bool `json:"duplicate"`
	QueuePosition int  `json:"queue_position"`
}

// Card mirrors the JSON payload Claude returns and the prep-app's
// questions schema.
type Card struct {
	Type    string   `json:"type"` // code | mcq | multi | short
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
	Result             string `json:"result"` // "right" | "wrong"
	Feedback           string `json:"feedback"`
	ModelAnswerSummary string `json:"model_answer_summary"`
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

// ---- Transform workflow types -------------------------------------------
//
// One workflow that handles two scopes:
//   • scope="card": improve a single question. Claude rewrites it and the
//     workflow auto-applies (no preview — blast radius is one row).
//   • scope="deck": apply a deck-wide transformation per the user's
//     free-text prompt. Workflow returns a Plan (modifications,
//     additions, deletions) and waits on a SignalApplyTransform or
//     SignalRejectTransform from the user before writing.

type TransformInput struct {
	UserID   string `json:"user_id"`
	Scope    string `json:"scope"`     // "card" | "deck"
	TargetID int    `json:"target_id"` // question_id (card) | deck_id (deck)
	Prompt   string `json:"prompt"`    // the user's free-text instruction
}

// CardModification is a full replacement of a card's user-visible fields.
// Claude returns the new state, not a diff, so the merge logic is simple.
type CardModification struct {
	QuestionID int      `json:"question_id"`
	Type       string   `json:"type"` // code|mcq|multi|short
	Topic      string   `json:"topic,omitempty"`
	Prompt     string   `json:"prompt"`
	Choices    []string `json:"choices,omitempty"`
	Answer     string   `json:"answer"`
	Rubric     string   `json:"rubric,omitempty"`
	Skeleton   string   `json:"skeleton,omitempty"`
	Language   string   `json:"language,omitempty"`
}

type TransformPlan struct {
	Scope         string             `json:"scope"`
	Modifications []CardModification `json:"modifications,omitempty"`
	Additions     []Card             `json:"additions,omitempty"`
	Deletions     []int              `json:"deletions,omitempty"`
	// Notes is a short human-readable summary of what claude decided to do
	// (e.g., "added skeletons to 5 cards"). Surfaced on the preview page.
	Notes string `json:"notes,omitempty"`
}

type TransformResult struct {
	ModifiedIDs []int `json:"modified_ids"`
	AddedIDs    []int `json:"added_ids"`
	DeletedIDs  []int `json:"deleted_ids"`
}

type TransformProgress struct {
	Scope      string           `json:"scope"`
	Status     string           `json:"status"` // "computing" | "awaiting_apply" | "applying" | "done" | "rejected" | "failed"
	StartedAt  string           `json:"started_at"`
	FinishedAt string           `json:"finished_at,omitempty"`
	Plan       *TransformPlan   `json:"plan,omitempty"`
	Result     *TransformResult `json:"result,omitempty"`
	Error      string           `json:"error,omitempty"`
}

// ComputeTransformInput is what the ComputeTransform activity takes.
type ComputeTransformInput struct {
	UserID   string `json:"user_id"`
	Scope    string `json:"scope"`
	TargetID int    `json:"target_id"`
	Prompt   string `json:"prompt"`
}

// ApplyTransformInput is the apply-step activity input. The plan is
// passed in directly so the activity is stateless re: workflow state.
type ApplyTransformInput struct {
	UserID string        `json:"user_id"`
	DeckID int           `json:"deck_id"` // needed for additions; 0 for card-scope
	Plan   TransformPlan `json:"plan"`
}

// ---- Plan-first generation workflow types -------------------------------
//
// New flow used at deck creation (and any future "generate cards" path):
//   1. Claude returns a list of brief PlanItems (titles + summaries, no full
//      content). Cheap call, ~5s.
//   2. The user reviews the list, optionally signals feedback (replan), and
//      eventually signals accept or reject.
//   3. On accept, each PlanItem is expanded into a full Card via parallel
//      activities, then inserted in order.

// PlanItem is a single brief card description. The full Card is generated
// only after the user accepts the plan.
type PlanItem struct {
	Title    string `json:"title"`
	Brief    string `json:"brief"`
	Type     string `json:"type,omitempty"` // claude's suggested type: code|mcq|multi|short
	Topic    string `json:"topic,omitempty"`
	Language string `json:"language,omitempty"` // for code items
}

type PlanGenerateInput struct {
	UserID   string `json:"user_id"`
	DeckID   int    `json:"deck_id"`
	DeckName string `json:"deck_name"`
	Prompt   string `json:"prompt"` // initial user prompt (== deck context_prompt)
}

type PlanGenerateResult struct {
	Status   string `json:"status"` // "completed" | "rejected" | "timed_out"
	AddedIDs []int  `json:"added_ids"`
}

type PlanGenerateProgress struct {
	Status         string              `json:"status"` // see below
	Round          int                 `json:"round"`  // increments each replan
	Plan           []PlanItem          `json:"plan,omitempty"`
	GeneratedCount int                 `json:"generated_count"` // cards built so far during "generating"
	Total          int                 `json:"total"`           // == len(plan) once accepted
	StartedAt      string              `json:"started_at"`
	FinishedAt     string              `json:"finished_at,omitempty"`
	Result         *PlanGenerateResult `json:"result,omitempty"`
	Error          string              `json:"error,omitempty"`
}

// Status values: "planning" | "awaiting_feedback" | "replanning" |
//                "generating" | "applying" | "done" | "rejected" | "failed"

type PlanCardsInput struct {
	UserID    string     `json:"user_id"`
	DeckName  string     `json:"deck_name"`
	Prompt    string     `json:"prompt"`               // deck description / topic
	PriorPlan []PlanItem `json:"prior_plan,omitempty"` // for replan rounds
	Feedback  string     `json:"feedback,omitempty"`   // for replan rounds
}

type GenerateCardFromBriefInput struct {
	UserID         string   `json:"user_id"`
	DeckName       string   `json:"deck_name"`
	DeckPrompt     string   `json:"deck_prompt"` // deck description, for grounding
	Item           PlanItem `json:"item"`
	Index          int      `json:"index"`
	Total          int      `json:"total"`
	IdempotencyKey string   `json:"idempotency_key"`
}
