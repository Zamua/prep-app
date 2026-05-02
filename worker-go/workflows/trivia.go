// TriviaGenerateWorkflow — generates a batch of short-answer trivia
// questions for a notification-driven deck.
//
// Flow:
//
//  1. ask the agent for N Q/A pairs on the deck's topic, dedupe-aware
//  2. for each returned pair, run InsertTriviaCard (idempotent — tags
//     each insert with the deck_id+prompt as the dedupe key, and writes
//     both the questions row AND the trivia_queue row in one tx)
//  3. expose progress via QueryTriviaProgress so the UI can poll
//
// Why a workflow + activity split (vs a single sync HTTP request):
//
//   - the agent call is 10-30s; blocking the FastAPI request thread is
//     bad UX and ties up a uvicorn worker
//   - if a transient db error hits one insert, we don't lose the whole
//     batch — temporal retries the single InsertTriviaCard activity
//   - the user can navigate away and come back — query handler still
//     answers
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

func TriviaGenerate(ctx workflow.Context, in shared.TriviaGenerateInput) (shared.TriviaGenerateResult, error) {
	if in.UserID == "" {
		return shared.TriviaGenerateResult{}, temporal.NewNonRetryableApplicationError(
			"user_id required", "BadInput", errors.New("user_id required"))
	}
	if in.DeckID == 0 {
		return shared.TriviaGenerateResult{}, temporal.NewNonRetryableApplicationError(
			"deck_id required", "BadInput", errors.New("deck_id"))
	}
	if in.Topic == "" {
		return shared.TriviaGenerateResult{}, temporal.NewNonRetryableApplicationError(
			"topic required", "BadInput", errors.New("topic"))
	}
	batchSize := in.BatchSize
	if batchSize <= 0 {
		batchSize = 25
	}

	progress := shared.TriviaGenerateProgress{
		Status:    "starting",
		Total:     batchSize,
		StartedAt: workflow.Now(ctx).UTC().Format(time.RFC3339),
	}
	if err := workflow.SetQueryHandler(ctx, shared.QueryTriviaProgress, func() (shared.TriviaGenerateProgress, error) {
		return progress, nil
	}); err != nil {
		return shared.TriviaGenerateResult{}, fmt.Errorf("register query: %w", err)
	}

	var a *activities.Activities

	// ---- Stage 1: ask agent for the batch ----
	progress.Status = "asking_claude"

	genOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    3,
			NonRetryableErrorTypes: []string{
				"BadInput", "BadTriviaJSON", "NoAgent",
			},
		},
	}
	genCtx := workflow.WithActivityOptions(ctx, genOpts)

	var pairs []shared.TriviaPair
	if err := workflow.ExecuteActivity(genCtx, a.GenerateTriviaBatch, shared.GenerateTriviaInput{
		UserID:    in.UserID,
		DeckID:    in.DeckID,
		Topic:     in.Topic,
		BatchSize: batchSize,
	}).Get(ctx, &pairs); err != nil {
		progress.Status = "failed"
		progress.Error = err.Error()
		progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
		return shared.TriviaGenerateResult{}, fmt.Errorf("generate batch: %w", err)
	}

	progress.Status = "inserting"
	progress.Total = len(pairs)

	// ---- Stage 2: insert each pair ----
	insertOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    500 * time.Millisecond,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    3,
		},
	}
	insertCtx := workflow.WithActivityOptions(ctx, insertOpts)

	for _, pair := range pairs {
		if pair.Q == "" || pair.A == "" {
			progress.SkippedInvalid++
			continue
		}
		var res shared.InsertTriviaCardResult
		err := workflow.ExecuteActivity(insertCtx, a.InsertTriviaCard, shared.InsertTriviaCardInput{
			UserID: in.UserID,
			DeckID: in.DeckID,
			Topic:  in.Topic,
			Prompt: pair.Q,
			Answer: pair.A,
		}).Get(ctx, &res)
		if err != nil {
			// Single-card failures are surfaced in progress but don't
			// abort the batch. Deck still gets the pairs that did land.
			workflow.GetLogger(ctx).Warn("insert trivia card failed",
				"deck_id", in.DeckID, "prompt", pair.Q, "err", err)
			progress.SkippedInvalid++
			continue
		}
		if res.Duplicate {
			progress.SkippedDups++
		} else {
			progress.Inserted++
		}
	}

	progress.Status = "done"
	progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
	return shared.TriviaGenerateResult{
		Inserted:       progress.Inserted,
		SkippedDups:    progress.SkippedDups,
		SkippedInvalid: progress.SkippedInvalid,
	}, nil
}
