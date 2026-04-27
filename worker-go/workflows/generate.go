// Package workflows holds the GenerateCardsWorkflow definition.
//
// The workflow is a single deterministic function — it CANNOT do I/O or
// random/wall-clock things directly. All side effects go through Activities.
// On worker restart Temporal replays the workflow code to rebuild state from
// history, so any non-determinism breaks the contract.
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
	// QueryGetProgress is the query name FastAPI calls to render the
	// generation status page.
	QueryGetProgress = "getProgress"
	// SignalCancel asks the workflow to stop after the current card.
	SignalCancel = "cancelGeneration"
)

// GenerateCards is the workflow function — registered under the name
// shared.WorkflowGenerate.
//
// Shape:
//   1. Prime a single Claude session with all deck context (one activity).
//   2. Loop count times:
//        a. Generate one card via the resumed session.
//        b. Insert it.
//        c. Update progress (workflow-local state, exposed via Query).
//        d. If cancel signal seen, break out gracefully.
//   3. Cleanup the session jsonl.
//   4. Notify Telegram.
func GenerateCards(ctx workflow.Context, in shared.GenerateCardsInput) (shared.GenerateCardsResult, error) {
	if in.Count < 1 || in.Count > 25 {
		return shared.GenerateCardsResult{}, temporal.NewNonRetryableApplicationError(
			"count out of bounds (1-25)", "BadInput", errors.New("invalid count"))
	}

	logger := workflow.GetLogger(ctx)
	wfInfo := workflow.GetInfo(ctx)
	// sessionID is now returned by PrimeClaudeSession (each prime mints its own
	// UUID to avoid "Session ID X is already in use" on retry).
	var sessionID string

	progress := shared.Progress{
		Total:     in.Count,
		Status:    "priming",
		StartedAt: workflow.Now(ctx).UTC().Format(time.RFC3339),
	}

	// Query handler — FastAPI polls this for the status page.
	if err := workflow.SetQueryHandler(ctx, QueryGetProgress, func() (shared.Progress, error) {
		return progress, nil
	}); err != nil {
		return shared.GenerateCardsResult{}, fmt.Errorf("register progress query: %w", err)
	}

	// Cancel signal channel.
	cancelCh := workflow.GetSignalChannel(ctx, SignalCancel)
	cancelled := false
	workflow.Go(ctx, func(c workflow.Context) {
		var ignored string
		cancelCh.Receive(c, &ignored)
		cancelled = true
	})

	// ---- Prime ----
	primeOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 90 * time.Second,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumInterval:    30 * time.Second,
			MaximumAttempts:    3,
			NonRetryableErrorTypes: []string{
				"BadDeckContext",
			},
		},
	}
	pctx := workflow.WithActivityOptions(ctx, primeOpts)
	var a *activities.Activities // typed nil — only the method names matter for registration
	var primeRes shared.PrimeResult
	if err := workflow.ExecuteActivity(pctx, a.PrimeClaudeSession, shared.PrimeInput{
		DeckName: in.DeckName,
	}).Get(ctx, &primeRes); err != nil {
		progress.Status = "failed"
		return shared.GenerateCardsResult{}, fmt.Errorf("prime: %w", err)
	}
	sessionID = primeRes.SessionID

	// ---- Per-card loop ----
	progress.Status = "generating"
	priorPrompts := []string{}
	inserted := []int{}

	cardOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 120 * time.Second,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumInterval:    30 * time.Second,
			MaximumAttempts:    3,
			NonRetryableErrorTypes: []string{
				"BadCardJSON", // bad model output — skip and continue, don't burn retries
			},
		},
	}
	insertOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 10 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    1 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    5,
		},
	}

	for i := 1; i <= in.Count; i++ {
		if cancelled {
			progress.Status = "cancelling"
			break
		}
		// Cap dedup tail to avoid prompt bloat.
		dedupTail := priorPrompts
		if len(dedupTail) > 8 {
			dedupTail = dedupTail[len(dedupTail)-8:]
		}
		key := fmt.Sprintf("%s-%d", wfInfo.WorkflowExecution.ID, i)

		var card shared.Card
		genCtx := workflow.WithActivityOptions(ctx, cardOpts)
		err := workflow.ExecuteActivity(genCtx, a.GenerateNextCard, shared.GenerateInput{
			SessionID:      sessionID,
			DeckName:       in.DeckName,
			Index:          i,
			Total:          in.Count,
			IdempotencyKey: key,
			PriorPrompts:   dedupTail,
		}).Get(ctx, &card)
		if err != nil {
			// Per-card failure isolation: log and continue.
			logger.Warn("card generation failed, skipping", "i", i, "err", err.Error())
			continue
		}

		var ins shared.InsertResult
		insCtx := workflow.WithActivityOptions(ctx, insertOpts)
		if err := workflow.ExecuteActivity(insCtx, a.InsertCard, shared.InsertInput{
			DeckName:       in.DeckName,
			IdempotencyKey: key,
			Card:           card,
		}).Get(ctx, &ins); err != nil {
			logger.Warn("insert failed, skipping", "i", i, "err", err.Error())
			continue
		}

		inserted = append(inserted, ins.CardID)
		priorPrompts = append(priorPrompts, card.Prompt)
		progress.Completed = i
		progress.CurrentTopic = card.Topic
		progress.LastCardAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
	}

	// ---- Cleanup (best-effort, don't fail the workflow over it) ----
	// Intentionally NO Telegram notification: the user follows progress via
	// the in-app polling page (templates/generation.html), not via push.
	cleanupCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 10 * time.Second,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 2},
	})
	_ = workflow.ExecuteActivity(cleanupCtx, a.Cleanup, shared.CleanupInput{
		SessionID: sessionID,
	}).Get(ctx, nil)

	if cancelled {
		progress.Status = "cancelled"
	} else {
		progress.Status = "done"
	}
	return shared.GenerateCardsResult{
		DeckName: in.DeckName,
		Inserted: inserted,
	}, nil
}

