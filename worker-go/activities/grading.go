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
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"prep-worker/shared"
)

// SRS interval ladder in minutes. Mirrors db.py:INTERVAL_LADDER_MINUTES.
// Wrong → step 0; right → step += 1, capped at len-1.
var srsLadderMinutes = []int{
	10,           // 10 min
	24 * 60,      // 1d
	3 * 24 * 60,  // 3d
	7 * 24 * 60,
	14 * 24 * 60,
	30 * 24 * 60,
}

// ---- Activity: GradeFreeText -------------------------------------------

// GradeFreeText runs the free-text grading prompt against claude.
// Returns a Verdict. Idempotent in the sense that grading the same input
// twice should produce nearly the same output (Claude is deterministic-ish
// at low temp); we don't write any side-effecting state in this activity.
func (a *Activities) GradeFreeText(ctx context.Context, in shared.GradeFreeTextInput) (shared.Verdict, error) {
	logger := activity.GetLogger(ctx)

	q, err := loadQuestion(a.Cfg.DBPath, in.QuestionID)
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

	prompt := fmt.Sprintf(`You are grading a flashcard answer for an interview-prep app. Be strict but fair.

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

	cmd := exec.CommandContext(ctx,
		a.Cfg.ClaudeBin,
		"--strict-mcp-config", "--mcp-config", emptyMCPConfig,
		"-p", prompt,
	)
	cmd.Env = os.Environ()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return shared.Verdict{}, fmt.Errorf("claude grade failed: %w (output: %s)", err, truncate(string(out), 800))
	}

	v, parseErr := parseVerdictJSON(out, q.Answer)
	if parseErr != nil {
		// Mark as wrong with the parse error in feedback — non-retryable so
		// the workflow doesn't burn retries on a model that consistently
		// returns malformed JSON.
		logger.Warn("verdict parse failed", "err", parseErr.Error(), "raw", truncate(string(out), 300))
		return shared.Verdict{
			Result:             "wrong",
			Feedback:           fmt.Sprintf("(grader returned non-JSON: %s)", truncate(string(out), 200)),
			ModelAnswerSummary: truncate(q.Answer, 400),
		}, nil
	}
	return v, nil
}

// ---- Activity: RecordReview -------------------------------------------

// RecordReview writes a row to the reviews table and advances the cards
// table's SRS step. Idempotent via grading_idempotency: if the
// idempotency key already exists, return the cached state.
func (a *Activities) RecordReview(ctx context.Context, in shared.RecordReviewInput) (shared.SRSState, error) {
	if in.Result != "right" && in.Result != "wrong" {
		return shared.SRSState{}, temporal.NewNonRetryableApplicationError(
			"invalid result", "BadResult",
			fmt.Errorf("result must be right or wrong, got %q", in.Result))
	}

	db, err := openDB(a.Cfg.DBPath)
	if err != nil {
		return shared.SRSState{}, err
	}
	defer db.Close()

	// Ensure the grading idempotency table exists. Do it here lazily rather
	// than coupling worker boot to schema setup. CREATE IF NOT EXISTS is cheap.
	if _, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS grading_idempotency (
			idempotency_key TEXT PRIMARY KEY,
			question_id     INTEGER NOT NULL,
			step            INTEGER NOT NULL,
			next_due        TEXT NOT NULL,
			interval_minutes INTEGER NOT NULL,
			created_at      TEXT NOT NULL
		);
	`); err != nil {
		return shared.SRSState{}, fmt.Errorf("create grading_idempotency: %w", err)
	}

	tx, err := db.Begin()
	if err != nil {
		return shared.SRSState{}, err
	}
	defer tx.Rollback()

	// Idempotency check.
	var existing shared.SRSState
	row := tx.QueryRow(`SELECT step, next_due, interval_minutes FROM grading_idempotency WHERE idempotency_key = ?`,
		in.IdempotencyKey)
	if err := row.Scan(&existing.Step, &existing.NextDue, &existing.IntervalMinutes); err == nil {
		// Already recorded — return the cached state, skip the writes.
		return existing, tx.Commit()
	} else if !errors.Is(err, sql.ErrNoRows) {
		return shared.SRSState{}, fmt.Errorf("idempotency check: %w", err)
	}

	// Read current step.
	var step int
	if err := tx.QueryRow(`SELECT step FROM cards WHERE question_id = ?`, in.QuestionID).Scan(&step); err != nil {
		return shared.SRSState{}, fmt.Errorf("read card step: %w", err)
	}

	// Advance.
	var newStep int
	if in.Result == "wrong" {
		newStep = 0
	} else {
		newStep = step + 1
		if newStep > len(srsLadderMinutes)-1 {
			newStep = len(srsLadderMinutes) - 1
		}
	}
	intervalMin := srsLadderMinutes[newStep]
	now := time.Now().UTC()
	nowISO := now.Format(time.RFC3339Nano)
	nextDue := now.Add(time.Duration(intervalMin) * time.Minute).Format(time.RFC3339Nano)

	if _, err := tx.Exec(`
		INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes)
		VALUES (?, ?, ?, ?, ?)`,
		in.QuestionID, nowISO, in.Result, in.UserAnswer, in.GraderNotes); err != nil {
		return shared.SRSState{}, fmt.Errorf("insert review: %w", err)
	}
	if _, err := tx.Exec(`
		UPDATE cards SET step = ?, next_due = ?, last_review = ? WHERE question_id = ?`,
		newStep, nextDue, nowISO, in.QuestionID); err != nil {
		return shared.SRSState{}, fmt.Errorf("update card: %w", err)
	}
	if _, err := tx.Exec(`
		INSERT INTO grading_idempotency (idempotency_key, question_id, step, next_due, interval_minutes, created_at)
		VALUES (?, ?, ?, ?, ?, ?)`,
		in.IdempotencyKey, in.QuestionID, newStep, nextDue, intervalMin, nowISO); err != nil {
		return shared.SRSState{}, fmt.Errorf("insert grading_idempotency: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return shared.SRSState{}, err
	}
	return shared.SRSState{
		Step:            newStep,
		NextDue:         nextDue,
		IntervalMinutes: intervalMin,
	}, nil
}

// ---- helpers -----------------------------------------------------------

type loadedQuestion struct {
	ID     int
	Type   string
	Prompt string
	Answer string
	Rubric string
}

func loadQuestion(dbPath string, qid int) (*loadedQuestion, error) {
	db, err := openDB(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	var q loadedQuestion
	var rubric sql.NullString
	err = db.QueryRow(`SELECT id, type, prompt, answer, rubric FROM questions WHERE id = ?`, qid).Scan(
		&q.ID, &q.Type, &q.Prompt, &q.Answer, &rubric)
	if err != nil {
		return nil, fmt.Errorf("question %d: %w", qid, err)
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
