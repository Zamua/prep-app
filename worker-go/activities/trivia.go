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

const _triviaBatchPromptTemplate = `You are generating short-answer trivia questions for a notification-driven flashcard app. Each card has a Q (the prompt), an A (the short answer), and an E (a deeper explanation revealed when the user taps "Deep dive").

Generate exactly %d questions on the topic:

%s

Constraints:
- Each question (q) fits in a phone notification body — <= 140 characters.
- Each answer (a) is 1-5 words. Names, numbers, short phrases. Not sentences.
- Each explanation (e) is 2-4 sentences. Surface the WHY: context, causation, why this matters, common misconception, or a memorable hook. Treat the user as smart and curious — go beyond restating the answer. ~300 characters is a good target.
- Cover varied sub-areas of the topic; don't all be the same flavor.
- Don't repeat any of these existing questions:

%s

Return ONLY valid JSON, no prose, no code fences. Format:

[
  {"q": "Question text?", "a": "Short answer", "e": "2-4 sentence explanation."},
  ...
]
`

// _triviaPlanPromptTemplate — claude returns titles + briefs only,
// no answers yet. Cheap call, used to seed the user's plan-review
// step before the (much more expensive) parallel expansion fan-out.
const _triviaPlanPromptTemplate = `You are planning a batch of short-answer trivia cards for a notification-driven flashcard app on the topic:

%s

Plan exactly %d cards. Don't generate full questions yet — just an outline. Each card gets:
- "title": a short label (3-8 words). What the card will be about.
- "brief": 1 sentence describing the angle / what the question will probe.

Cover varied sub-areas of the topic; don't pile up around the same flavor. Diversity beats depth at this stage.

Don't repeat any of these existing cards (already in the deck):

%s

Return ONLY a JSON array, no prose, no fences:

[
  {"title": "Soundtrack composer", "brief": "Identify the composer of the original soundtrack."},
  ...
]
`

const _triviaPlanReplanPromptTemplate = `You previously proposed this trivia plan for the topic "%s":

%s

The user gave this feedback:

%s

Revise the plan accordingly. Same shape as before — JSON array of {title, brief} objects, %d items. Only the JSON, no prose.
`

const _triviaExpandPromptTemplate = `Generate ONE short-answer trivia card for the topic "%s".

The card was planned as:
  title: %s
  brief: %s

Write the FULL card content. Output a single JSON object (no prose, no fences) with these fields:

- "q": the question text. Fits in a phone notification body — <= 140 characters.
- "a": the short answer. 1-5 words. Names, numbers, short phrases. Not sentences.
- "e": a 2-4 sentence explanation. Surface the WHY — context, causation, common misconception, memorable hook. Treat the user as smart and curious. ~300 characters is a good target.

Output ONLY the JSON object.
`

// PlanTriviaBatch asks claude for an outline (titles + briefs) of the
// next batch. Cheap call (claude doesn't write answers), feeds the
// awaiting_feedback step where the user can replan / accept / reject
// before the expensive expansion fan-out fires.
func (a *Activities) PlanTriviaBatch(ctx context.Context, in shared.PlanTriviaBatchInput) ([]shared.TriviaPlanItem, error) {
	if a.Cfg.Agent == nil {
		return nil, noAgentErr("PlanTriviaBatch")
	}
	existing, err := loadExistingTriviaPrompts(a.Cfg.DBPath, in.DeckID)
	if err != nil {
		return nil, fmt.Errorf("load existing prompts: %w", err)
	}

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
				activity.RecordHeartbeat(ctx, "planning trivia batch")
			}
		}
	}()

	batch := in.BatchSize
	if batch <= 0 {
		batch = 25
	}

	var prompt string
	if len(in.PriorPlan) == 0 {
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
		prompt = fmt.Sprintf(_triviaPlanPromptTemplate, strings.TrimSpace(in.Topic), batch, existingBlock)
	} else {
		// Replan: include the prior plan + user feedback. Existing
		// prompts not re-passed; the prior plan already accounts for
		// dedupe and the user's feedback supersedes it anyway.
		var b strings.Builder
		for _, item := range in.PriorPlan {
			b.WriteString("- ")
			b.WriteString(item.Title)
			b.WriteString(": ")
			b.WriteString(item.Brief)
			b.WriteString("\n")
		}
		prompt = fmt.Sprintf(_triviaPlanReplanPromptTemplate,
			strings.TrimSpace(in.Topic), b.String(), strings.TrimSpace(in.Feedback), batch)
	}

	out, err := a.Cfg.Agent.Run(ctx, agent.RunInput{Prompt: prompt})
	if err != nil {
		return nil, fmt.Errorf("agent trivia plan failed: %w", err)
	}

	plan, err := parseTriviaPlanJSON(out.Stdout)
	if err != nil {
		return nil, temporal.NewNonRetryableApplicationError(
			fmt.Sprintf("plan JSON parse failed: %v; head=%q", err, truncate(out.Stdout, 300)),
			"BadTriviaPlanJSON", err)
	}
	return plan, nil
}

// GenerateTriviaCardFromBrief expands one plan item into a full
// q/a/e via claude. Designed to run in parallel with siblings (each
// call is independent — no shared session, no shared state).
func (a *Activities) GenerateTriviaCardFromBrief(ctx context.Context, in shared.GenerateTriviaCardFromBriefInput) (shared.TriviaPair, error) {
	if a.Cfg.Agent == nil {
		return shared.TriviaPair{}, noAgentErr("GenerateTriviaCardFromBrief")
	}
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
				activity.RecordHeartbeat(ctx, fmt.Sprintf("expanding %d/%d", in.Index+1, in.Total))
			}
		}
	}()

	prompt := fmt.Sprintf(_triviaExpandPromptTemplate,
		strings.TrimSpace(in.Topic), in.Item.Title, in.Item.Brief)
	out, err := a.Cfg.Agent.Run(ctx, agent.RunInput{Prompt: prompt})
	if err != nil {
		return shared.TriviaPair{}, fmt.Errorf("agent trivia expand failed: %w", err)
	}

	pair, err := parseSingleTriviaPair(out.Stdout)
	if err != nil {
		return shared.TriviaPair{}, temporal.NewNonRetryableApplicationError(
			fmt.Sprintf("expand JSON parse failed: %v; head=%q", err, truncate(out.Stdout, 300)),
			"BadTriviaCardJSON", err)
	}
	return pair, nil
}

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
	explanation := strings.TrimSpace(in.Explanation)
	if prompt == "" || answer == "" {
		return shared.InsertTriviaCardResult{}, temporal.NewNonRetryableApplicationError(
			"prompt + answer required", "BadInput", nil)
	}
	return insertTriviaCard(a.Cfg.DBPath, in.UserID, in.DeckID, in.Topic, prompt, answer, explanation)
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

func insertTriviaCard(dbPath, userID string, deckID int, topic, prompt, answer, explanation string) (shared.InsertTriviaCardResult, error) {
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
		INSERT INTO questions (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language, explanation)
		VALUES (?, ?, 'short', ?, ?, NULL, ?, NULL, ?, NULL, NULL, ?)`,
		userID, deckID, nullable(topic), prompt, answer, now, nullable(explanation))
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

// parseTriviaPlanJSON: same fence-tolerant array extraction as
// parseTriviaJSON, just decodes into PlanItems.
func parseTriviaPlanJSON(stdout string) ([]shared.TriviaPlanItem, error) {
	text := strings.TrimSpace(stdout)
	text = _triviaCodeFenceHead.ReplaceAllString(text, "")
	text = _triviaCodeFenceTail.ReplaceAllString(text, "")
	start := strings.Index(text, "[")
	end := strings.LastIndex(text, "]")
	if start < 0 || end < 0 || end < start {
		return nil, fmt.Errorf("no JSON array")
	}
	var plan []shared.TriviaPlanItem
	if err := json.Unmarshal([]byte(text[start:end+1]), &plan); err != nil {
		return nil, fmt.Errorf("unmarshal: %w", err)
	}
	// Drop entries missing required fields — dont error the whole plan
	// over a single garbage row.
	out := make([]shared.TriviaPlanItem, 0, len(plan))
	for _, item := range plan {
		if strings.TrimSpace(item.Title) == "" || strings.TrimSpace(item.Brief) == "" {
			continue
		}
		out = append(out, item)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("no usable plan items in %d-element array", len(plan))
	}
	return out, nil
}

// parseSingleTriviaPair: one object, fence-tolerant.
func parseSingleTriviaPair(stdout string) (shared.TriviaPair, error) {
	text := strings.TrimSpace(stdout)
	text = _triviaCodeFenceHead.ReplaceAllString(text, "")
	text = _triviaCodeFenceTail.ReplaceAllString(text, "")
	start := strings.Index(text, "{")
	end := strings.LastIndex(text, "}")
	if start < 0 || end < 0 || end < start {
		return shared.TriviaPair{}, fmt.Errorf("no JSON object")
	}
	var pair shared.TriviaPair
	if err := json.Unmarshal([]byte(text[start:end+1]), &pair); err != nil {
		return shared.TriviaPair{}, fmt.Errorf("unmarshal: %w", err)
	}
	if strings.TrimSpace(pair.Q) == "" || strings.TrimSpace(pair.A) == "" {
		return shared.TriviaPair{}, fmt.Errorf("missing q or a")
	}
	return pair, nil
}
