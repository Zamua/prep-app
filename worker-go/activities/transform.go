// Transform activities — the worker-side implementation of the
// "improve a card" / "transform a deck" feature.
//
// Two activities:
//   ComputeTransform — shells out to claude with the current card(s) +
//     the user's prompt; parses claude's JSON response into a Plan.
//   ApplyTransform   — writes the Plan to the DB in a single transaction
//     (modifications via UPDATE, additions via INSERT, deletions via DELETE
//     which CASCADES through cards/reviews per existing FKs).
//
// The workflow decides whether to auto-apply (card scope) or wait for a
// user signal before applying (deck scope).
package activities

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"prep-worker/agent"
	"prep-worker/shared"
)

// ---- Activity: ComputeTransform ----------------------------------------

func (a *Activities) ComputeTransform(ctx context.Context, in shared.ComputeTransformInput) (shared.TransformPlan, error) {
	if in.UserID == "" {
		return shared.TransformPlan{}, temporal.NewNonRetryableApplicationError(
			"user_id required", "BadInput", errors.New("user_id required"))
	}
	if in.Scope != "card" && in.Scope != "deck" {
		return shared.TransformPlan{}, temporal.NewNonRetryableApplicationError(
			"unknown scope", "BadInput", fmt.Errorf("scope=%q", in.Scope))
	}
	if strings.TrimSpace(in.Prompt) == "" {
		return shared.TransformPlan{}, temporal.NewNonRetryableApplicationError(
			"prompt required", "BadInput", errors.New("prompt required"))
	}

	prompt, err := a.buildTransformPrompt(in)
	if err != nil {
		return shared.TransformPlan{}, temporal.NewNonRetryableApplicationError(
			"build prompt failed", "BadInput", err)
	}

	// Heartbeat so Temporal doesn't time us out while claude is thinking.
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
				activity.RecordHeartbeat(ctx, "transforming")
			}
		}
	}()

	if a.Cfg.Agent == nil {
		return shared.TransformPlan{}, noAgentErr("ComputeTransform")
	}
	out, err := a.Cfg.Agent.Run(ctx, agent.RunInput{Prompt: prompt})
	if err != nil {
		return shared.TransformPlan{}, fmt.Errorf("agent transform failed: %w", err)
	}

	plan, err := parseTransformPlan([]byte(out.Stdout))
	if err != nil {
		return shared.TransformPlan{}, fmt.Errorf("parse plan: %w (raw: %s)", err, truncate(out.Stdout, 600))
	}
	plan.Scope = in.Scope
	return plan, nil
}

// buildTransformPrompt constructs the prompt sent to `claude -p`. Loads
// the relevant card(s) from the DB and frames the user's request.
func (a *Activities) buildTransformPrompt(in shared.ComputeTransformInput) (string, error) {
	if in.Scope == "card" {
		cards, err := loadCardForTransform(a.Cfg.DBPath, in.UserID, in.TargetID)
		if err != nil {
			return "", err
		}
		if len(cards) == 0 {
			return "", fmt.Errorf("question %d not found for user %s", in.TargetID, in.UserID)
		}
		return cardScopePrompt(cards[0], in.Prompt), nil
	}

	cards, err := loadDeckCardsForTransform(a.Cfg.DBPath, in.UserID, in.TargetID)
	if err != nil {
		return "", err
	}
	// Empty deck is fine: the user can transform an empty deck into a
	// populated one (the action is just "claude, generate everything from
	// scratch per my prompt"). Pass an empty list and let claude return
	// pure additions. The route already verified the deck row exists.
	return deckScopePrompt(cards, in.Prompt), nil
}

func cardScopePrompt(card cardForTransform, userPrompt string) string {
	cardJSON, _ := json.MarshalIndent(card, "", "  ")
	return fmt.Sprintf(`You are improving a single flashcard in a spaced-repetition learning app, per the user's request.

**Current card (JSON):**
`+"```json"+`
%s
`+"```"+`

**User's request:**
%s

If URLs or recent material are referenced, you may use your web-fetch / web-search tools to ground the change.

Return a JSON object describing the new state of THIS card. Shape:

`+"```json"+`
{
  "modifications": [{
    "question_id": <id>,
    "type": "code|mcq|multi|short",
    "topic": "...",
    "prompt": "...",
    "choices": ["..."],         // omit for code/short
    "answer": "...",
    "rubric": "...",
    "skeleton": "...",          // optional starter code for code questions
    "language": "..."           // optional, only for code (go|java|python|...)
  }],
  "notes": "<one short sentence summarizing what changed>"
}
`+"```"+`

Preserve fields the user's request didn't ask to change. Output ONLY the JSON object, no commentary or fences.`,
		string(cardJSON), userPrompt)
}

func deckScopePrompt(cards []cardForTransform, userPrompt string) string {
	// json.MarshalIndent(nil) = "null"; we want "[]" so an empty deck
	// reads as a clean empty list to claude.
	if cards == nil {
		cards = []cardForTransform{}
	}
	cardsJSON, _ := json.MarshalIndent(cards, "", "  ")
	return fmt.Sprintf(`You are applying a deck-wide transformation to a spaced-repetition flashcard deck, per the user's request.

**Current deck (JSON array of cards):**
`+"```json"+`
%s
`+"```"+`

**User's request:**
%s

If URLs or recent material are referenced, you may use your web-fetch / web-search tools to ground the change.

Return a JSON object describing the changes to apply. Only include cards that actually need to change. Shape:

`+"```json"+`
{
  "modifications": [
    {"question_id": <id>, "type": "...", "topic": "...", "prompt": "...", "choices": [...], "answer": "...", "rubric": "...", "skeleton": "...", "language": "..."}
  ],
  "additions": [
    {"type": "code|mcq|multi|short", "topic": "...", "prompt": "...", "choices": [...], "answer": "...", "rubric": "...", "skeleton": "...", "language": "..."}
  ],
  "deletions": [<question_id>, ...],
  "notes": "<one short sentence summarizing the overall change>"
}
`+"```"+`

Output ONLY the JSON object, no commentary or fences. If the request asks for fewer than 1 change, return empty arrays. Cap additions at 15 cards per request.`,
		string(cardsJSON), userPrompt)
}

func parseTransformPlan(out []byte) (shared.TransformPlan, error) {
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
	var plan shared.TransformPlan
	if err := json.Unmarshal([]byte(raw), &plan); err != nil {
		return shared.TransformPlan{}, fmt.Errorf("not JSON: %w", err)
	}
	return plan, nil
}

// ---- Activity: ApplyTransform ------------------------------------------

func (a *Activities) ApplyTransform(ctx context.Context, in shared.ApplyTransformInput) (shared.TransformResult, error) {
	if in.UserID == "" {
		return shared.TransformResult{}, temporal.NewNonRetryableApplicationError(
			"user_id required", "BadInput", errors.New("user_id required"))
	}
	db, err := openDB(a.Cfg.DBPath)
	if err != nil {
		return shared.TransformResult{}, err
	}
	defer db.Close()

	tx, err := db.Begin()
	if err != nil {
		return shared.TransformResult{}, err
	}
	defer tx.Rollback()

	res := shared.TransformResult{}

	// ---- Modifications ----
	for _, m := range in.Plan.Modifications {
		// Defense-in-depth: confirm the card belongs to this user before mutating.
		var owner string
		err := tx.QueryRow(`SELECT user_id FROM questions WHERE id = ?`, m.QuestionID).Scan(&owner)
		if err == sql.ErrNoRows {
			continue // silently skip — claude may have hallucinated an id
		}
		if err != nil {
			return shared.TransformResult{}, fmt.Errorf("read owner of qid %d: %w", m.QuestionID, err)
		}
		if owner != in.UserID {
			continue
		}
		choicesJSON := jsonOrNull(m.Choices)
		_, err = tx.Exec(`
			UPDATE questions
			   SET type = COALESCE(NULLIF(?, ''), type),
			       topic = ?,
			       prompt = COALESCE(NULLIF(?, ''), prompt),
			       choices = ?,
			       answer = COALESCE(NULLIF(?, ''), answer),
			       rubric = ?,
			       skeleton = ?,
			       language = ?
			 WHERE id = ? AND user_id = ?`,
			m.Type, nullIfEmpty(m.Topic), m.Prompt, choicesJSON,
			m.Answer, nullIfEmpty(m.Rubric),
			nullIfEmpty(m.Skeleton), nullIfEmpty(m.Language),
			m.QuestionID, in.UserID,
		)
		if err != nil {
			return shared.TransformResult{}, fmt.Errorf("update qid %d: %w", m.QuestionID, err)
		}
		res.ModifiedIDs = append(res.ModifiedIDs, m.QuestionID)
	}

	// ---- Additions ----
	for _, c := range in.Plan.Additions {
		choicesJSON := jsonOrNull(c.Choices)
		var skel, lang sql.NullString
		if c.Type == "code" {
			if c.Skeleton != "" {
				skel = sql.NullString{String: c.Skeleton, Valid: true}
			}
			if c.Language != "" {
				lang = sql.NullString{String: c.Language, Valid: true}
			}
		}
		r, err := tx.Exec(`
			INSERT INTO questions (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			in.UserID, in.DeckID, c.Type, nullIfEmpty(c.Topic), c.Prompt, choicesJSON,
			c.Answer, nullIfEmpty(c.Rubric), nowISO(), skel, lang,
		)
		if err != nil {
			return shared.TransformResult{}, fmt.Errorf("insert addition: %w", err)
		}
		id64, _ := r.LastInsertId()
		cardID := int(id64)
		if _, err := tx.Exec(`INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)`,
			cardID, nowISO()); err != nil {
			return shared.TransformResult{}, fmt.Errorf("insert cards row: %w", err)
		}
		res.AddedIDs = append(res.AddedIDs, cardID)
	}

	// ---- Deletions ----
	// Per-FK CASCADE handles cards + reviews automatically.
	for _, qid := range in.Plan.Deletions {
		r, err := tx.Exec(`DELETE FROM questions WHERE id = ? AND user_id = ?`, qid, in.UserID)
		if err != nil {
			return shared.TransformResult{}, fmt.Errorf("delete qid %d: %w", qid, err)
		}
		n, _ := r.RowsAffected()
		if n > 0 {
			res.DeletedIDs = append(res.DeletedIDs, qid)
		}
	}

	if err := tx.Commit(); err != nil {
		return shared.TransformResult{}, err
	}
	return res, nil
}

// ---- helpers -----------------------------------------------------------

// cardForTransform is the shape we pass to claude — a subset of the row
// that's relevant to rewriting. Excludes srs state, timestamps, and ids
// the model doesn't need to think about.
type cardForTransform struct {
	QuestionID int      `json:"question_id"`
	Type       string   `json:"type"`
	Topic      string   `json:"topic,omitempty"`
	Prompt     string   `json:"prompt"`
	Choices    []string `json:"choices,omitempty"`
	Answer     string   `json:"answer"`
	Rubric     string   `json:"rubric,omitempty"`
	Skeleton   string   `json:"skeleton,omitempty"`
	Language   string   `json:"language,omitempty"`
}

func loadCardForTransform(dbPath, userID string, qid int) ([]cardForTransform, error) {
	db, err := openDB(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	row := db.QueryRow(`
		SELECT id, type, COALESCE(topic, ''), prompt, choices, answer,
		       COALESCE(rubric, ''), COALESCE(skeleton, ''), COALESCE(language, '')
		  FROM questions WHERE id = ? AND user_id = ?`, qid, userID)
	c, err := scanCardForTransform(row)
	if err != nil {
		return nil, err
	}
	return []cardForTransform{c}, nil
}

func loadDeckCardsForTransform(dbPath, userID string, deckID int) ([]cardForTransform, error) {
	db, err := openDB(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	rows, err := db.Query(`
		SELECT id, type, COALESCE(topic, ''), prompt, choices, answer,
		       COALESCE(rubric, ''), COALESCE(skeleton, ''), COALESCE(language, '')
		  FROM questions WHERE deck_id = ? AND user_id = ? AND COALESCE(suspended, 0) = 0
		 ORDER BY id`, deckID, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []cardForTransform
	for rows.Next() {
		c, err := scanCardForTransform(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanCardForTransform(r rowScanner) (cardForTransform, error) {
	var c cardForTransform
	var choicesJSON sql.NullString
	if err := r.Scan(&c.QuestionID, &c.Type, &c.Topic, &c.Prompt,
		&choicesJSON, &c.Answer, &c.Rubric, &c.Skeleton, &c.Language); err != nil {
		return cardForTransform{}, err
	}
	if choicesJSON.Valid && choicesJSON.String != "" {
		_ = json.Unmarshal([]byte(choicesJSON.String), &c.Choices)
	}
	return c, nil
}

func jsonOrNull(items []string) any {
	if len(items) == 0 {
		return nil
	}
	b, _ := json.Marshal(items)
	return string(b)
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
