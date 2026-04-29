package workflows

import (
	"errors"
	"fmt"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"

	"prep-worker/activities"
	"prep-worker/shared"
)

const (
	// QueryGetGradeProgress is the query name FastAPI's /grading/{wid}/status
	// endpoint calls to render the polling page.
	QueryGetGradeProgress = "getGradeProgress"
)

// GradeAnswer is the workflow function — registered under the name
// shared.WorkflowGrade.
//
// Two-step shape:
//  1. GradeFreeText activity — claude -p shell-out, returns Verdict
//  2. RecordReview activity   — writes review row + advances SRS, returns SRSState
//
// State exposed via the getGradeProgress query so the polling page can
// render "grading…" → "recording…" → "done" + verdict + state.
func GradeAnswer(ctx workflow.Context, in shared.GradeAnswerInput) (shared.GradeAnswerResult, error) {
	if in.QuestionID <= 0 {
		return shared.GradeAnswerResult{}, temporal.NewNonRetryableApplicationError(
			"invalid question_id", "BadInput", errors.New("question_id required"))
	}

	wfInfo := workflow.GetInfo(ctx)
	progress := shared.GradeProgress{
		Status:    "grading",
		StartedAt: workflow.Now(ctx).UTC().Format(time.RFC3339),
	}

	if err := workflow.SetQueryHandler(ctx, QueryGetGradeProgress, func() (shared.GradeProgress, error) {
		return progress, nil
	}); err != nil {
		return shared.GradeAnswerResult{}, fmt.Errorf("register progress query: %w", err)
	}

	var a *activities.Activities

	// ---- Grade ----
	gradeOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 90 * time.Second,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumInterval:    30 * time.Second,
			MaximumAttempts:    3,
			NonRetryableErrorTypes: []string{
				"BadQuestionID",
			},
		},
	}
	gctx := workflow.WithActivityOptions(ctx, gradeOpts)
	var verdict shared.Verdict
	if err := workflow.ExecuteActivity(gctx, a.GradeFreeText, shared.GradeFreeTextInput{
		QuestionID: in.QuestionID,
		UserAnswer: in.UserAnswer,
		IDK:        in.IDK,
		UserID:     in.UserID,
	}).Get(ctx, &verdict); err != nil {
		progress.Status = "failed"
		return shared.GradeAnswerResult{}, fmt.Errorf("grade: %w", err)
	}

	// ---- Record ----
	progress.Status = "recording"
	recordOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 10 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    1 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    5,
			NonRetryableErrorTypes: []string{
				"BadResult",
			},
		},
	}
	rctx := workflow.WithActivityOptions(ctx, recordOpts)
	var state shared.SRSState
	if err := workflow.ExecuteActivity(rctx, a.RecordReview, shared.RecordReviewInput{
		QuestionID:     in.QuestionID,
		UserID:         in.UserID,
		Result:         verdict.Result,
		UserAnswer:     in.UserAnswer,
		GraderNotes:    verdict.Feedback,
		IdempotencyKey: wfInfo.WorkflowExecution.ID,
	}).Get(ctx, &state); err != nil {
		progress.Status = "failed"
		return shared.GradeAnswerResult{}, fmt.Errorf("record: %w", err)
	}

	result := shared.GradeAnswerResult{
		QuestionID: in.QuestionID,
		UserAnswer: in.UserAnswer,
		IDK:        in.IDK,
		Verdict:    verdict,
		State:      state,
	}
	progress.Status = "done"
	progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
	progress.Result = &result
	return result, nil
}
