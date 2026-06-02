// Grading activities for the GradeAnswerWorkflow.
//
// Mirrors what's in the Python prep-app's grader.py + db.py:record_review,
// ported to Go so the Temporal worker can run them as durable activities
// instead of blocking the FastAPI request thread.
//
// Mcq/multi grading stays in Python (it's deterministic and instant).
// These Go activities only handle code/short — the slow path that needs
// claude -p shell-out.

package activities

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"prep-worker/agent"
	"prep-worker/shared"
)

// ---- Activity: GradeFreeText -------------------------------------------

// GradeFreeText runs the free-text grading prompt against claude.
// Returns a Verdict. Idempotent in the sense that grading the same input
// twice should produce nearly the same output (Claude is deterministic-ish
// at low temp); we don't write any side-effecting state in this activity.
func (a *Activities) GradeFreeText(ctx context.Context, in shared.GradeFreeTextInput) (shared.Verdict, error) {
	logger := activity.GetLogger(ctx)

	q, err := loadQuestion(a.Cfg.DBPath, in.UserID, in.QuestionID)
	if err != nil {
		return shared.Verdict{}, temporal.NewNonRetryableApplicationError(
			"load question failed", "BadQuestionID", err)
	}

	// Short-circuit "I don't know" — no Claude call needed.
	if in.IDK {
		return shared.Verdict{
			Result:             "wrong",
			Feedback:           "Marked as 'I don't know' — see again soon.",
			ModelAnswerSummary: truncate(q.Answer, 400),
		}, nil
	}

	rubric := q.Rubric
	if rubric == "" {
		rubric = "(no explicit rubric — judge against the model answer)"
	}

	prompt := fmt.Sprintf(`You are grading a flashcard answer in a spaced-repetition learning app. Be strict but fair.

**Question type:** %s
**Prompt:**
%s

**Model answer:**
%s

**Rubric (what a correct answer must demonstrate):**
%s

**User's answer:**
%s

Decide: is the user's answer substantively correct? Partial credit counts as wrong (we'll re-show it soon). For `+"`code`"+` questions, accept any correct approach — don't require the exact syntax of the model answer.

Output a single JSON object (no prose, no fences) with:
- "result": "right" or "wrong"
- "feedback": 1-3 sentences of feedback the user will see. Be concrete: name what they got/missed.
- "model_answer_summary": 1-2 sentence summary of the model answer for the user to compare against.

Output ONLY the JSON object.`,
		q.Type, q.Prompt, q.Answer, rubric, in.UserAnswer)

	// Heartbeat while claude runs.
	done := make(chan struct{})
	defer close(done)
	go func() {
		t := time.NewTicker(10 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-done:
				return
			case <-t.C:
				activity.RecordHeartbeat(ctx, fmt.Sprintf("grading qid=%d", in.QuestionID))
			}
		}
	}()

	if a.Cfg.Agent == nil {
		return shared.Verdict{}, noAgentErr("GradeFreeText")
	}
	out, err := a.Cfg.Agent.Run(ctx, agent.RunInput{Prompt: prompt, UserID: in.UserID})
	if err != nil {
		return shared.Verdict{}, fmt.Errorf("agent grade failed: %w", err)
	}

	v, parseErr := parseVerdictJSON([]byte(out.Stdout), q.Answer)
	if parseErr != nil {
		// Mark as wrong with the parse error in feedback — non-retryable so
		// the workflow doesn't burn retries on a model that consistently
		// returns malformed JSON.
		logger.Warn("verdict parse failed", "err", parseErr.Error(), "raw", truncate(out.Stdout, 300))
		return shared.Verdict{
			Result:             "wrong",
			Feedback:           fmt.Sprintf("(grader returned non-JSON: %s)", truncate(out.Stdout, 200)),
			ModelAnswerSummary: truncate(q.Answer, 400),
		}, nil
	}
	return v, nil
}

// ---- Activity: RecordReview -------------------------------------------

// RecordReview posts to prep's /api/internal/record-review endpoint to
// write the review row and advance FSRS state for the card. Python owns
// the scheduler (prep/domain/srs.py); doing the math worker-side would
// require porting FSRS twice + keeping the weights in sync, so we
// delegate via the same internal-HTTP pattern as /api/agent/run.
//
// Idempotent on `IdempotencyKey` (usually the workflow id) — the
// Python helper short-circuits a duplicate write and returns the
// cached SRSState. Same `grading_idempotency` table as before; the
// ownership of the writes flipped over, not the schema.
func (a *Activities) RecordReview(ctx context.Context, in shared.RecordReviewInput) (shared.SRSState, error) {
	if in.Result != "right" && in.Result != "wrong" {
		return shared.SRSState{}, temporal.NewNonRetryableApplicationError(
			"invalid result", "BadResult",
			fmt.Errorf("result must be right or wrong, got %q", in.Result))
	}

	baseURL := strings.TrimSpace(os.Getenv("PREP_AGENT_URL"))
	if baseURL == "" {
		return shared.SRSState{}, fmt.Errorf("PREP_AGENT_URL not set — worker can't reach /api/internal/record-review")
	}
	token := strings.TrimSpace(os.Getenv("PREP_INTERNAL_TOKEN"))
	if token == "" {
		return shared.SRSState{}, fmt.Errorf("PREP_INTERNAL_TOKEN not set — internal endpoint would refuse the call")
	}

	body, err := json.Marshal(in)
	if err != nil {
		return shared.SRSState{}, fmt.Errorf("marshal record-review request: %w", err)
	}

	// /api/internal/record-review lives next to /api/agent/run, so the
	// base path is the same (PREP_AGENT_URL points at /api/agent — we
	// strip the trailing /run/agent and rebuild). The deploy env var is
	// historically named for the agent endpoint, hence the rewrite.
	url := strings.TrimSuffix(strings.TrimRight(baseURL, "/"), "/api/agent") + "/api/internal/record-review"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return shared.SRSState{}, fmt.Errorf("build record-review request: %w", err)
	}
	req.Header.Set("content-type", "application/json")
	req.Header.Set("X-Internal-Token", token)

	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return shared.SRSState{}, fmt.Errorf("record-review http: %w", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode == http.StatusBadRequest {
		// Bad owner / unknown result / missing card → non-retryable.
		var errBody struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(raw, &errBody)
		return shared.SRSState{}, temporal.NewNonRetryableApplicationError(
			"record-review rejected", "BadInput", fmt.Errorf("%s", errBody.Error))
	}
	if resp.StatusCode/100 != 2 {
		return shared.SRSState{}, fmt.Errorf("record-review http %d: %s", resp.StatusCode, truncate(string(raw), 400))
	}
	var out shared.SRSState
	if err := json.Unmarshal(raw, &out); err != nil {
		return shared.SRSState{}, fmt.Errorf("parse record-review response: %w (raw: %s)", err, truncate(string(raw), 400))
	}
	return out, nil
}

// ---- helpers -----------------------------------------------------------

type loadedQuestion struct {
	ID     int
	Type   string
	Prompt string
	Answer string
	Rubric string
}

func loadQuestion(dbPath, userID string, qid int) (*loadedQuestion, error) {
	db, err := openDB(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	var q loadedQuestion
	var rubric sql.NullString
	err = db.QueryRow(`SELECT id, type, prompt, answer, rubric FROM questions WHERE id = ? AND user_id = ?`,
		qid, userID).Scan(&q.ID, &q.Type, &q.Prompt, &q.Answer, &rubric)
	if err != nil {
		return nil, fmt.Errorf("question %d for user %s: %w", qid, userID, err)
	}
	if rubric.Valid {
		q.Rubric = rubric.String
	}
	return &q, nil
}

func parseVerdictJSON(out []byte, modelAnswer string) (shared.Verdict, error) {
	raw := strings.TrimSpace(string(out))
	raw = strings.TrimPrefix(raw, "```json")
	raw = strings.TrimPrefix(raw, "```")
	raw = strings.TrimSuffix(raw, "```")
	raw = strings.TrimSpace(raw)
	if i := strings.Index(raw, "{"); i > 0 {
		raw = raw[i:]
	}
	if i := strings.LastIndex(raw, "}"); i >= 0 && i < len(raw)-1 {
		raw = raw[:i+1]
	}

	var v struct {
		Result             string `json:"result"`
		Feedback           string `json:"feedback"`
		ModelAnswerSummary string `json:"model_answer_summary"`
	}
	if err := json.Unmarshal([]byte(raw), &v); err != nil {
		return shared.Verdict{}, fmt.Errorf("not JSON: %w", err)
	}
	if v.Result != "right" && v.Result != "wrong" {
		v.Result = "wrong"
	}
	if v.ModelAnswerSummary == "" {
		v.ModelAnswerSummary = truncate(modelAnswer, 400)
	}
	return shared.Verdict{
		Result:             v.Result,
		Feedback:           v.Feedback,
		ModelAnswerSummary: v.ModelAnswerSummary,
	}, nil
}
