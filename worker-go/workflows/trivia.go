// TriviaGenerateWorkflow — straight-shot batch generation for a
// notification-driven trivia deck. Linear, no plan / iterate gate:
//
//	GENERATING — single claude call returns N {q, a, e} pairs.
//	APPLYING   — InsertTriviaCard per pair (existing activity, with
//	             idempotency via deck_id+normalized prompt).
//	DONE / FAILED — terminal.
//
// The earlier flow walked the user through a plan-review step (claude
// sketches an outline; user replans/accepts/rejects) before expanding
// each item with a per-card claude call. That added latency and a
// gating UX that wasn't pulling its weight — the user wants to type
// a topic and get a deck back. The plan/expand activities + the
// trivia feedback/accept/reject signals are still registered but
// unused by this workflow; left in place rather than deleted so the
// signal endpoints don't break for any in-flight workflow runs from
// the previous shape. They're dead code from this workflow's POV
// and can be removed in a follow-up.
//
// Progress query exposes Status + Total + GeneratedCount + Inserted
// so the polling page renders a real progress bar.
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
		Status:    "generating",
		Total:     batchSize,
		StartedAt: workflow.Now(ctx).UTC().Format(time.RFC3339),
	}
	if err := workflow.SetQueryHandler(ctx, shared.QueryTriviaProgress, func() (shared.TriviaGenerateProgress, error) {
		return progress, nil
	}); err != nil {
		return shared.TriviaGenerateResult{}, fmt.Errorf("register query: %w", err)
	}

	var a *activities.Activities

	// ---- GENERATING ----
	genOpts := workflow.ActivityOptions{
		// Ceiling sits 1m above the HTTP client's 30m timeout so a stuck
		// claude call surfaces as an HTTP deadline error rather than a
		// temporal activity timeout.
		StartToCloseTimeout: 31 * time.Minute,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			// No retries on claude calls — surface failure to the user.
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    1,
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
		return shared.TriviaGenerateResult{}, fmt.Errorf("generate: %w", err)
	}

	if len(pairs) == 0 {
		progress.Status = "failed"
		progress.Error = "claude returned 0 cards"
		progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
		return shared.TriviaGenerateResult{}, fmt.Errorf("0 trivia cards generated")
	}

	progress.GeneratedCount = len(pairs)
	progress.Total = len(pairs) // claude may return fewer than requested; reflect actual

	// ---- APPLYING ----
	progress.Status = "applying"
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
		var res shared.InsertTriviaCardResult
		err := workflow.ExecuteActivity(insertCtx, a.InsertTriviaCard, shared.InsertTriviaCardInput{
			UserID:      in.UserID,
			DeckID:      in.DeckID,
			Topic:       in.Topic,
			Prompt:      pair.Q,
			Answer:      pair.A,
			Explanation: pair.E,
		}).Get(ctx, &res)
		if err != nil {
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
		Status:         "completed",
		Inserted:       progress.Inserted,
		SkippedDups:    progress.SkippedDups,
		SkippedInvalid: progress.SkippedInvalid,
	}, nil
}
