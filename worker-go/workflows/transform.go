// Transform workflow — replaces the count-based "generate N cards" flow
// with a free-form prompt the user types into the deck page (or a
// per-card "improve" form). Two scopes:
//   - scope="card": auto-applies the rewrite once claude returns it
//   - scope="deck": returns a Plan via query, waits for an apply or
//     reject signal from the user before writing to the DB
//
// State exposed via `getTransformProgress` query so the polling page can
// render "computing…" → "awaiting apply" → "applying…" → "done".
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
	// How long the workflow will wait for an apply/reject signal in deck
	// scope before timing out and treating the run as rejected. 1h is
	// generous given a user might leave the preview tab open and come
	// back.
	transformApplyTimeout = 1 * time.Hour
)

func Transform(ctx workflow.Context, in shared.TransformInput) (shared.TransformResult, error) {
	if in.Scope != "card" && in.Scope != "deck" && in.Scope != "reorganize" {
		return shared.TransformResult{}, temporal.NewNonRetryableApplicationError(
			"unknown scope", "BadInput", fmt.Errorf("scope=%q", in.Scope))
	}
	if in.UserID == "" {
		return shared.TransformResult{}, temporal.NewNonRetryableApplicationError(
			"user_id required", "BadInput", errors.New("user_id required"))
	}

	progress := shared.TransformProgress{
		Scope:     in.Scope,
		Status:    "computing",
		StartedAt: workflow.Now(ctx).UTC().Format(time.RFC3339),
	}
	if err := workflow.SetQueryHandler(ctx, shared.QueryTransformProgress, func() (shared.TransformProgress, error) {
		return progress, nil
	}); err != nil {
		return shared.TransformResult{}, fmt.Errorf("register progress query: %w", err)
	}

	var a *activities.Activities

	// ---- Compute the plan ----
	computeOpts := workflow.ActivityOptions{
		// Generous: claude reads the entire deck and rewrites multiple cards.
		// Activity heartbeats every 10s so a truly hung run still gets killed
		// by the heartbeat timeout — this is just the upper bound.
		StartToCloseTimeout: 15 * time.Minute,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumInterval:    30 * time.Second,
			MaximumAttempts:    2,
			NonRetryableErrorTypes: []string{
				"BadInput",
			},
		},
	}
	cctx := workflow.WithActivityOptions(ctx, computeOpts)
	var plan shared.TransformPlan
	if err := workflow.ExecuteActivity(cctx, a.ComputeTransform, shared.ComputeTransformInput{
		UserID:            in.UserID,
		Scope:             in.Scope,
		TargetID:          in.TargetID,
		Prompt:            in.Prompt,
		DeckContextPrompt: in.DeckContextPrompt,
	}).Get(ctx, &plan); err != nil {
		progress.Status = "failed"
		progress.Error = err.Error()
		return shared.TransformResult{}, fmt.Errorf("compute: %w", err)
	}
	progress.Plan = &plan

	// For deck scope we need the deck_id to insert any additions. The
	// workflow input gives us TargetID = deck_id directly.
	deckID := 0
	if in.Scope == "deck" {
		deckID = in.TargetID
	}

	// ---- Card scope: auto-apply ----
	if in.Scope == "card" {
		return applyAndFinish(ctx, &progress, in.UserID, deckID, plan)
	}

	// ---- Deck scope: wait for apply/reject signal ----
	progress.Status = "awaiting_apply"

	applyCh := workflow.GetSignalChannel(ctx, shared.SignalApplyTransform)
	rejectCh := workflow.GetSignalChannel(ctx, shared.SignalRejectTransform)

	timer := workflow.NewTimer(ctx, transformApplyTimeout)

	var doApply bool
	sel := workflow.NewSelector(ctx)
	sel.AddReceive(applyCh, func(c workflow.ReceiveChannel, more bool) {
		var sig struct{}
		c.Receive(ctx, &sig)
		doApply = true
	})
	sel.AddReceive(rejectCh, func(c workflow.ReceiveChannel, more bool) {
		var sig struct{}
		c.Receive(ctx, &sig)
		doApply = false
	})
	sel.AddFuture(timer, func(f workflow.Future) {
		// Timer fired — treat as reject.
		_ = f.Get(ctx, nil)
		doApply = false
	})
	sel.Select(ctx)

	if !doApply {
		// Transient state: the moment we receive the reject signal we
		// flip to "rejecting" so the HTTP layer (which renders the
		// fragment immediately after sending the signal) sees the truth
		// of the workflow rather than reading a stale "awaiting_apply"
		// or having to long-poll for terminal completion. Even though
		// reject has no cleanup work today, the transient state gives
		// future cleanup a place to live and gives the UI a moment of
		// honest "we're processing your decision" feedback.
		//
		// A 1ms timer yields control back to the worker so queries can
		// observe the "rejecting" status before the workflow closes;
		// without a yield, this state is set and overwritten within a
		// single workflow task and no query can ever see it.
		progress.Status = "rejecting"
		_ = workflow.Sleep(ctx, time.Millisecond)
		progress.Status = "rejected"
		progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
		return shared.TransformResult{}, nil
	}

	// Transient state: flip to "applying" the instant the apply signal
	// arrives, BEFORE applyAndFinish kicks off its activity. Without
	// this, a query racing between signal-receipt and applyAndFinish's
	// own status set would see the stale "awaiting_apply" and the UI
	// would render the accept/reject buttons twice.
	progress.Status = "applying"
	return applyAndFinish(ctx, &progress, in.UserID, deckID, plan)
}

func applyAndFinish(ctx workflow.Context, progress *shared.TransformProgress,
	userID string, deckID int, plan shared.TransformPlan) (shared.TransformResult, error) {
	// Idempotent: caller may have already set this to "applying" before
	// invoking us (deck scope, post-signal). Card scope falls through
	// directly without going via the signal path, so it needs the set
	// here too.
	progress.Status = "applying"
	applyOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    1 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    3,
			NonRetryableErrorTypes: []string{
				"BadInput",
			},
		},
	}
	actx := workflow.WithActivityOptions(ctx, applyOpts)
	var a *activities.Activities
	var result shared.TransformResult
	if err := workflow.ExecuteActivity(actx, a.ApplyTransform, shared.ApplyTransformInput{
		UserID: userID,
		DeckID: deckID,
		Plan:   plan,
	}).Get(ctx, &result); err != nil {
		progress.Status = "failed"
		progress.Error = err.Error()
		return shared.TransformResult{}, fmt.Errorf("apply: %w", err)
	}
	progress.Status = "done"
	progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
	progress.Result = &result
	return result, nil
}
