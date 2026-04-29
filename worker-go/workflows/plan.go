// Plan-first card generation workflow.
//
// Two phases:
//
//   PLANNING — claude returns a brief outline (titles + 1-2 sentence
//   briefs). The user reviews via the polling page. They can:
//     * Send feedback ("split that one", "add 2 more on X") — replan.
//     * Accept — move to expansion.
//     * Reject — abandon.
//     * Walk away — 24h timer treats as reject.
//
//   EXPANSION — for each accepted PlanItem, spawn one parallel activity
//   that asks claude to write the full card content. Insert each one
//   as it returns; failed expansions are skipped (don't block siblings).
//
// Why parallel: each per-card prompt is small (deck description + brief),
// so cache-miss-per-call is cheap. Wall-clock to expand 12 cards is then
// roughly 1 card's time, not 12.
//
// State exposed via QueryPlanProgress so the polling page can show the
// current phase, the live plan (across replans), and the running
// expansion count.

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
	// How long the workflow waits for an accept/reject signal before
	// timing out (treated as reject). Generous; users may step away
	// after planning and come back later.
	planSignalTimeout = 24 * time.Hour
)

func PlanGenerate(ctx workflow.Context, in shared.PlanGenerateInput) (shared.PlanGenerateResult, error) {
	if in.UserID == "" {
		return shared.PlanGenerateResult{}, temporal.NewNonRetryableApplicationError(
			"user_id required", "BadInput", errors.New("user_id required"))
	}
	if in.DeckID == 0 || in.DeckName == "" {
		return shared.PlanGenerateResult{}, temporal.NewNonRetryableApplicationError(
			"deck_id + deck_name required", "BadInput", errors.New("deck"))
	}
	if in.Prompt == "" {
		return shared.PlanGenerateResult{}, temporal.NewNonRetryableApplicationError(
			"prompt required (the deck description seeds claude)", "BadInput", errors.New("prompt"))
	}

	progress := shared.PlanGenerateProgress{
		Status:    "planning",
		StartedAt: workflow.Now(ctx).UTC().Format(time.RFC3339),
	}
	if err := workflow.SetQueryHandler(ctx, shared.QueryPlanProgress, func() (shared.PlanGenerateProgress, error) {
		return progress, nil
	}); err != nil {
		return shared.PlanGenerateResult{}, fmt.Errorf("register query: %w", err)
	}

	var a *activities.Activities

	planOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    2,
			NonRetryableErrorTypes: []string{
				"BadInput", "BadPlanJSON", "NoAgent",
			},
		},
	}
	planCtx := workflow.WithActivityOptions(ctx, planOpts)

	// ---- Initial plan ----
	var plan []shared.PlanItem
	if err := workflow.ExecuteActivity(planCtx, a.PlanCards, shared.PlanCardsInput{
		UserID:   in.UserID,
		DeckName: in.DeckName,
		Prompt:   in.Prompt,
	}).Get(ctx, &plan); err != nil {
		progress.Status = "failed"
		progress.Error = err.Error()
		return shared.PlanGenerateResult{}, fmt.Errorf("initial plan: %w", err)
	}
	progress.Plan = plan
	progress.Round = 1
	progress.Status = "awaiting_feedback"

	// ---- Wait on signals: feedback (replan) | accept | reject | timeout ----
	feedbackCh := workflow.GetSignalChannel(ctx, shared.SignalPlanFeedback)
	acceptCh := workflow.GetSignalChannel(ctx, shared.SignalPlanAccept)
	rejectCh := workflow.GetSignalChannel(ctx, shared.SignalPlanReject)

	// Single timer started here; if the user replans, we keep the same
	// 24h budget rather than refreshing it (otherwise an attacker /
	// bored user could pin a worker indefinitely).
	timeoutTimer := workflow.NewTimer(ctx, planSignalTimeout)

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
			var newPlan []shared.PlanItem
			err := workflow.ExecuteActivity(planCtx, a.PlanCards, shared.PlanCardsInput{
				UserID:    in.UserID,
				DeckName:  in.DeckName,
				Prompt:    in.Prompt,
				PriorPlan: plan,
				Feedback:  fb,
			}).Get(ctx, &newPlan)
			if err != nil {
				// Replan failure is recoverable: keep prior plan, surface
				// the error, let the user try again.
				progress.Error = fmt.Sprintf("replan failed: %v", err)
				progress.Status = "awaiting_feedback"
				return
			}
			plan = newPlan
			progress.Plan = plan
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
		res := &shared.PlanGenerateResult{Status: "rejected"}
		progress.Result = res
		return *res, nil
	}

	// ---- Expansion: bounded-parallel claude calls, batched ----
	//
	// We process the plan items in batches of `expandBatchSize`. Each
	// claude invocation in the agent container is a `claude -p` shell
	// process that loads the SDK and makes an Anthropic API call;
	// running 20 of them in parallel against a small docker host blew
	// the container's memory and got most of them OOM-killed (`signal:
	// killed`) before they could heartbeat — Sean's first plan-first
	// 20-card deck only landed 2 cards.
	//
	// Batched fan-out keeps wall-clock decent (4 simultaneous claude
	// calls) without overwhelming the agent's resource footprint. The
	// workflow stays deterministic (no Go channels — Temporal needs
	// workflow ops to be replay-safe).
	const expandBatchSize = 4

	progress.Status = "generating"
	progress.Total = len(plan)
	progress.GeneratedCount = 0

	wfID := workflow.GetInfo(ctx).WorkflowExecution.ID

	expandOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumInterval:    30 * time.Second,
			MaximumAttempts:    2,
			NonRetryableErrorTypes: []string{
				"BadCardJSON", "NoAgent",
			},
		},
	}
	exCtx := workflow.WithActivityOptions(ctx, expandOpts)

	type expansion struct {
		index int
		fut   workflow.Future
	}

	cards := make([]shared.Card, 0, len(plan))
	for batchStart := 0; batchStart < len(plan); batchStart += expandBatchSize {
		batchEnd := batchStart + expandBatchSize
		if batchEnd > len(plan) {
			batchEnd = len(plan)
		}

		// Schedule this batch all at once, then drain it. The next
		// batch doesn't start until every member of this one has
		// landed (success or failure) — Temporal's Future.Get serves
		// as the synchronization barrier.
		batch := make([]expansion, 0, batchEnd-batchStart)
		for i := batchStart; i < batchEnd; i++ {
			fut := workflow.ExecuteActivity(exCtx, a.GenerateCardFromBrief, shared.GenerateCardFromBriefInput{
				UserID:         in.UserID,
				DeckName:       in.DeckName,
				DeckPrompt:     in.Prompt,
				Item:           plan[i],
				Index:          i,
				Total:          len(plan),
				IdempotencyKey: fmt.Sprintf("%s-expand-%d", wfID, i),
			})
			batch = append(batch, expansion{index: i, fut: fut})
		}

		for _, e := range batch {
			var c shared.Card
			if err := e.fut.Get(ctx, &c); err != nil {
				// Skip failed expansion; don't fail the whole workflow
				// on one bad card. The plan list will be slightly
				// smaller than the user expected — surfaced via
				// GeneratedCount.
				workflow.GetLogger(ctx).Warn("expand failed",
					"index", e.index, "err", err.Error())
				continue
			}
			cards = append(cards, c)
			progress.GeneratedCount = len(cards)
		}
	}

	if len(cards) == 0 {
		progress.Status = "failed"
		progress.Error = "every card expansion failed"
		return shared.PlanGenerateResult{}, fmt.Errorf("0 cards expanded successfully")
	}

	// ---- Apply: insert each card. Reuses the existing InsertCard activity. ----
	progress.Status = "applying"

	insertOpts := workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval: 1 * time.Second,
			MaximumAttempts: 3,
		},
	}
	insCtx := workflow.WithActivityOptions(ctx, insertOpts)

	added := make([]int, 0, len(cards))
	for i, c := range cards {
		var ir shared.InsertResult
		err := workflow.ExecuteActivity(insCtx, a.InsertCard, shared.InsertInput{
			DeckName:       in.DeckName,
			UserID:         in.UserID,
			IdempotencyKey: fmt.Sprintf("%s-insert-%d", wfID, i),
			Card:           c,
		}).Get(ctx, &ir)
		if err != nil {
			workflow.GetLogger(ctx).Warn("insert failed", "index", i, "err", err.Error())
			continue
		}
		if !ir.Duplicate {
			added = append(added, ir.CardID)
		}
	}

	res := &shared.PlanGenerateResult{Status: "completed", AddedIDs: added}
	progress.Status = "done"
	progress.FinishedAt = workflow.Now(ctx).UTC().Format(time.RFC3339)
	progress.Result = res
	return *res, nil
}
