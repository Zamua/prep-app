// Activities for the TriviaGenerate workflow.
//
//   GenerateTriviaBatch — single agent call returning N short-Q-short-A
//   pairs. Tolerates code-fence-wrapped output (matches the prep.trivia
//   service's parser). Errors classified as BadTriviaJSON are
//   non-retryable so the workflow surfaces "claude returned garbage" to
//   the user instead of looping.
//
//   InsertTriviaCard — write one (questions row + trivia_queue row) in
//   one transaction. Idempotent on (deck_id, normalized prompt) so a
//   retry doesn't double-insert.

package activities

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"prep-worker/agent"
	"prep-worker/shared"
)

const _triviaBatchPromptTemplate = `You are generating short-answer trivia questions for a notification-driven flashcard app. The user gets one question at a time on their phone and types a brief free-form answer.

Generate exactly %d questions on the topic:

%s

Constraints:
- Each question fits in a phone notification body — <= 140 characters.
- Each answer is 1-5 words. Names, numbers, short phrases. Not sentences.
- Cover varied sub-areas of the topic; don't all be the same flavor.
- Don't repeat any of these existing questions:

%s

Return ONLY valid JSON, no prose, no code fences. Format:

[
  {"q": "Question text?", "a": "Short answer"},
  ...
]
`

// GenerateTriviaBatch asks the agent for the batch.
func (a *Activities) GenerateTriviaBatch(ctx context.Context, in shared.GenerateTriviaInput) ([]shared.TriviaPair, error) {
	if a.Cfg.Agent == nil {
		return nil, noAgentErr("GenerateTriviaBatch")
	}

	// Pull current existing prompts from the deck for the dedupe block.
	// Activities are allowed to do small db reads — keeps the workflow
	// pure-orchestration.
	existing, err := loadExistingTriviaPrompts(a.Cfg.DBPath, in.DeckID)
	if err != nil {
		return nil, fmt.Errorf("load existing prompts: %w", err)
	}

	// Heartbeat so a stuck claude call doesn't pin the worker silently.
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
				activity.RecordHeartbeat(ctx, "generating trivia batch")
			}
		}
	}()

	existingBlock := "(none yet — this is the first batch)"
	if len(existing) > 0 {
		var b strings.Builder
		for i, p := range existing {
			if i >= 200 {
				break
			}
			b.WriteString("- ")
			b.WriteString(p)
			b.WriteString("\n")
		}
		existingBlock = b.String()
	}

	batch := in.BatchSize
	if batch <= 0 {
		batch = 25
	}
	prompt := fmt.Sprintf(_triviaBatchPromptTemplate, batch, strings.TrimSpace(in.Topic), existingBlock)

	out, err := a.Cfg.Agent.Run(ctx, agent.RunInput{Prompt: prompt})
	if err != nil {
		return nil, fmt.Errorf("agent trivia gen failed: %w", err)
	}

	pairs, err := parseTriviaJSON(out.Stdout)
	if err != nil {
		return nil, temporal.NewNonRetryableApplicationError(
			fmt.Sprintf("agent returned unparseable JSON: %v; head=%q", err, truncate(out.Stdout, 300)),
			"BadTriviaJSON", err)
	}
	return pairs, nil
}

// InsertTriviaCard writes one question + queue row.
func (a *Activities) InsertTriviaCard(ctx context.Context, in shared.InsertTriviaCardInput) (shared.InsertTriviaCardResult, error) {
	prompt := strings.TrimSpace(in.Prompt)
	answer := strings.TrimSpace(in.Answer)
	if prompt == "" || answer == "" {
		return shared.InsertTriviaCardResult{}, temporal.NewNonRetryableApplicationError(
			"prompt + answer required", "BadInput", nil)
	}
	return insertTriviaCard(a.Cfg.DBPath, in.UserID, in.DeckID, in.Topic, prompt, answer)
}

// ---- helpers ----------------------------------------------------------

func loadExistingTriviaPrompts(dbPath string, deckID int) ([]string, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("open db: %w", err)
	}
	defer db.Close()

	rows, err := db.Query(`
		SELECT prompt FROM questions WHERE deck_id = ? ORDER BY id`, deckID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []string
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

func insertTriviaCard(dbPath, userID string, deckID int, topic, prompt, answer string) (shared.InsertTriviaCardResult, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return shared.InsertTriviaCardResult{}, fmt.Errorf("open db: %w", err)
	}
	defer db.Close()
	if _, err := db.Exec("PRAGMA foreign_keys = ON"); err != nil {
		return shared.InsertTriviaCardResult{}, err
	}

	tx, err := db.Begin()
	if err != nil {
		return shared.InsertTriviaCardResult{}, err
	}
	defer tx.Rollback()

	// Dedupe on (deck_id, prompt). Cheap normalize: lowercased trimmed
	// string compare. Avoids a second batch repeating questions verbatim.
	normPrompt := strings.ToLower(strings.TrimSpace(prompt))
	var existingID int
	err = tx.QueryRow(`
		SELECT id FROM questions
		WHERE deck_id = ? AND LOWER(TRIM(prompt)) = ?`,
		deckID, normPrompt).Scan(&existingID)
	if err == nil {
		// Duplicate — return the existing id so retries are no-ops.
		var qp int
		_ = tx.QueryRow(`SELECT queue_position FROM trivia_queue WHERE question_id = ?`, existingID).Scan(&qp)
		_ = tx.Commit()
		return shared.InsertTriviaCardResult{QuestionID: existingID, Duplicate: true, QueuePosition: qp}, nil
	} else if err != sql.ErrNoRows {
		return shared.InsertTriviaCardResult{}, fmt.Errorf("dedupe lookup: %w", err)
	}

	now := nowISO()
	res, err := tx.Exec(`
		INSERT INTO questions (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language)
		VALUES (?, ?, 'short', ?, ?, NULL, ?, NULL, ?, NULL, NULL)`,
		userID, deckID, nullable(topic), prompt, answer, now)
	if err != nil {
		return shared.InsertTriviaCardResult{}, fmt.Errorf("insert questions: %w", err)
	}
	qid64, _ := res.LastInsertId()
	qid := int(qid64)

	// Append at the back of the deck's queue (queue_position = max + 1).
	var maxPos sql.NullInt64
	if err := tx.QueryRow(`
		SELECT COALESCE(MAX(tq.queue_position), 0)
		FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id
		WHERE q.deck_id = ?`, deckID).Scan(&maxPos); err != nil {
		return shared.InsertTriviaCardResult{}, fmt.Errorf("max queue_position: %w", err)
	}
	nextPos := int(maxPos.Int64) + 1
	if _, err := tx.Exec(`
		INSERT INTO trivia_queue (question_id, queue_position) VALUES (?, ?)`,
		qid, nextPos); err != nil {
		return shared.InsertTriviaCardResult{}, fmt.Errorf("insert trivia_queue: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return shared.InsertTriviaCardResult{}, err
	}
	return shared.InsertTriviaCardResult{QuestionID: qid, Duplicate: false, QueuePosition: nextPos}, nil
}

// parseTriviaJSON tolerates code-fence wrapping and leading prose,
// matching prep.trivia.service._parse_qa_pairs's contract on the
// Python side.
var _triviaCodeFenceHead = regexp.MustCompile("(?i)^```(?:json)?\\s*")
var _triviaCodeFenceTail = regexp.MustCompile("\\s*```\\s*$")

func parseTriviaJSON(stdout string) ([]shared.TriviaPair, error) {
	text := strings.TrimSpace(stdout)
	text = _triviaCodeFenceHead.ReplaceAllString(text, "")
	text = _triviaCodeFenceTail.ReplaceAllString(text, "")
	start := strings.Index(text, "[")
	end := strings.LastIndex(text, "]")
	if start < 0 || end < 0 || end < start {
		return nil, fmt.Errorf("no JSON array")
	}
	chunk := text[start : end+1]
	var pairs []shared.TriviaPair
	if err := json.Unmarshal([]byte(chunk), &pairs); err != nil {
		return nil, fmt.Errorf("unmarshal: %w", err)
	}
	return pairs, nil
}
