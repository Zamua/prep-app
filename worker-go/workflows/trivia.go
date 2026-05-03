// TriviaGenerateWorkflow — plan-then-expand generation for a
// notification-driven trivia deck. Mirrors the SRS PlanGenerate
// workflow's shape so the UX is consistent.
//
// Phases:
//
//	PLANNING — claude returns a brief outline ([]TriviaPlanItem,
//	  title + 1-sentence brief each). Cheap call, ~5s.
//	AWAITING_FEEDBACK — user reviews; can replan ("more from the
//	  multiplayer era"), accept, or reject. 24h timer treats walk-
//	  away as reject.
//	GENERATING — for each accepted item, parallel claude expansions
//	  (batched to 4 concurrent — same memory-pressure cap as
//	  PlanGenerate's expansion). Each call returns a full q/a/e.
//	APPLYING — InsertTriviaCard per pair (existing activity, with
//	  idempotency via deck_id+normalized prompt).
//	DONE / REJECTED / FAILED — terminal.
//
// Progress query exposes Status + Plan + Round + GeneratedCount/Total
// so the polling page can render a real progress bar instead of a
// three-state spinner.
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
	triviaSignalTimeout = 24 * time.Hour
	triviaExpandBatch   = 4 // concurrent claude expansions; same cap as PlanGenerate
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
		Status:    "planning",
		Round:     1,
		StartedAt: workflow.Now(ctx).UTC().Format(time.RFC3339),
	}
	if err := workflow.SetQueryHandler(ctx, shared.QueryTriviaProgress, func() (shared.TriviaGenerateProgress, error) {
		return progress, nil
	}); err != nil {
		return shared.TriviaGenerateResult{}, fmt.Errorf("register query: %w", err)
	}

	var a *activities.Activities

	// ---- PLAN ----
	planOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    2,
			NonRetryableErrorTypes: []string{
				"BadInput", "BadTriviaPlanJSON", "NoAgent",
			},
		},
	}
	planCtx := workflow.WithActivityOptions(ctx, planOpts)

	var plan []shared.TriviaPlanItem
	if err := workflow.ExecuteActivity(planCtx, a.PlanTriviaBatch, shared.PlanTriviaBatchInput{
		UserID:    in.UserID,
		DeckID:    in.DeckID,
		Topic:     in.Topic,
		BatchSize: batchSize,
	}).Get(ctx, &plan); err != nil {
		progress.Status = "failed"
		progress.Error = err.Error()
		progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
		return shared.TriviaGenerateResult{}, fmt.Errorf("initial plan: %w", err)
	}
	progress.Plan = plan
	progress.Total = len(plan)
	progress.Status = "awaiting_feedback"

	// ---- AWAITING FEEDBACK ----
	feedbackCh := workflow.GetSignalChannel(ctx, shared.SignalTriviaFeedback)
	acceptCh := workflow.GetSignalChannel(ctx, shared.SignalTriviaAccept)
	rejectCh := workflow.GetSignalChannel(ctx, shared.SignalTriviaReject)
	timeoutTimer := workflow.NewTimer(ctx, triviaSignalTimeout)

	var (
		accepted bool
		decided  bool
	)
	for !decided {
		sel := workflow.NewSelector(ctx)

		sel.AddReceive(feedbackCh, func(c workflow.ReceiveChannel, _ bool) {
			var fb string
			c.Receive(ctx, &fb)
			progress.Status = "replanning"
			var newPlan []shared.TriviaPlanItem
			err := workflow.ExecuteActivity(planCtx, a.PlanTriviaBatch, shared.PlanTriviaBatchInput{
				UserID:    in.UserID,
				DeckID:    in.DeckID,
				Topic:     in.Topic,
				BatchSize: batchSize,
				PriorPlan: plan,
				Feedback:  fb,
			}).Get(ctx, &newPlan)
			if err != nil {
				// Replan failure: keep the old plan visible, surface
				// the error so the user can try again or accept what
				// they have.
				progress.Error = fmt.Sprintf("replan failed: %v", err)
				progress.Status = "awaiting_feedback"
				return
			}
			plan = newPlan
			progress.Plan = plan
			progress.Total = len(plan)
			progress.Round++
			progress.Error = ""
			progress.Status = "awaiting_feedback"
		})
		sel.AddReceive(acceptCh, func(c workflow.ReceiveChannel, _ bool) {
			var sig struct{}
			c.Receive(ctx, &sig)
			accepted = true
			decided = true
		})
		sel.AddReceive(rejectCh, func(c workflow.ReceiveChannel, _ bool) {
			var sig struct{}
			c.Receive(ctx, &sig)
			decided = true
		})
		sel.AddFuture(timeoutTimer, func(f workflow.Future) {
			_ = f.Get(ctx, nil)
			decided = true
		})

		sel.Select(ctx)
	}

	if !accepted {
		progress.Status = "rejected"
		progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
		return shared.TriviaGenerateResult{Status: "rejected"}, nil
	}

	// ---- GENERATING (parallel expansion, batched) ----
	progress.Status = "generating"
	progress.GeneratedCount = 0

	expandOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumInterval:    30 * time.Second,
			MaximumAttempts:    2,
			NonRetryableErrorTypes: []string{
				"BadTriviaCardJSON", "NoAgent",
			},
		},
	}
	exCtx := workflow.WithActivityOptions(ctx, expandOpts)

	wfID := workflow.GetInfo(ctx).WorkflowExecution.ID
	type expansion struct {
		index int
		item  shared.TriviaPlanItem
		fut   workflow.Future
	}

	pairs := make([]shared.TriviaPair, 0, len(plan))
	for batchStart := 0; batchStart < len(plan); batchStart += triviaExpandBatch {
		batchEnd := batchStart + triviaExpandBatch
		if batchEnd > len(plan) {
			batchEnd = len(plan)
		}
		batch := make([]expansion, 0, batchEnd-batchStart)
		for i := batchStart; i < batchEnd; i++ {
			fut := workflow.ExecuteActivity(exCtx, a.GenerateTriviaCardFromBrief,
				shared.GenerateTriviaCardFromBriefInput{
					UserID:         in.UserID,
					DeckID:         in.DeckID,
					Topic:          in.Topic,
					Item:           plan[i],
					Index:          i,
					Total:          len(plan),
					IdempotencyKey: fmt.Sprintf("%s-expand-%d", wfID, i),
				})
			batch = append(batch, expansion{index: i, item: plan[i], fut: fut})
		}
		for _, e := range batch {
			var pair shared.TriviaPair
			if err := e.fut.Get(ctx, &pair); err != nil {
				workflow.GetLogger(ctx).Warn("trivia expand failed",
					"index", e.index, "title", e.item.Title, "err", err.Error())
				progress.SkippedInvalid++
				continue
			}
			pairs = append(pairs, pair)
			progress.GeneratedCount = len(pairs)
		}
	}

	if len(pairs) == 0 {
		progress.Status = "failed"
		progress.Error = "every card expansion failed"
		progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
		return shared.TriviaGenerateResult{}, fmt.Errorf("0 trivia cards expanded")
	}

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
