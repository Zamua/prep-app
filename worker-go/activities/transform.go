// Transform activities — the worker-side implementation of the
// "improve a card" / "transform a deck" feature.
//
// Two activities:
//
//	ComputeTransform — shells out to claude with the current card(s) +
//	  the user's prompt; parses claude's JSON response into a Plan.
//	ApplyTransform   — writes the Plan to the DB in a single transaction
//	  (modifications via UPDATE, additions via INSERT, deletions via DELETE
//	  which CASCADES through cards/reviews per existing FKs).
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
	if in.Scope == "reorganize" {
		decks, err := loadAllUserDecksForTransform(a.Cfg.DBPath, in.UserID)
		if err != nil {
			return "", err
		}
		return reorganizeScopePrompt(decks, in.Prompt), nil
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
    "language": "...",          // optional, only for code (go|java|python|...)
    "explanation": "...",       // trivia only — 2-4 sentence "deep dive"
    "answer_regex": "..."       // trivia only — case-insensitive fullmatch
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
    {"question_id": <id>, "type": "...", "topic": "...", "prompt": "...", "choices": [...], "answer": "...", "rubric": "...", "skeleton": "...", "language": "...", "explanation": "...", "answer_regex": "..."}
  ],
  "additions": [
    {"type": "code|mcq|multi|short", "topic": "...", "prompt": "...", "choices": [...], "answer": "...", "rubric": "...", "skeleton": "...", "language": "...", "explanation": "...", "answer_regex": "..."}
  ],
  "deletions": [<question_id>, ...],
  "notes": "<one short sentence summarizing the overall change>"
}
`+"```"+`

Field guidance:
- explanation + answer_regex are TRIVIA-only (the cards in the input JSON will only have them set if this is a trivia deck). They surface as a "Deep dive" disclosure (explanation, 2-4 sentences) and the first-pass grader regex (answer_regex, case-insensitive fullmatch). If you change a card's prompt/answer, ALSO update explanation + answer_regex to stay in sync — a stale regex matching the old answer will silently mis-grade.
- For srs cards, leave explanation and answer_regex empty.
- Preserve fields the user's request didn't ask to change.

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

	// ---- Cross-deck preamble ----
	// Build a name → deck_id map so reorganize ops (additions with
	// dest_deck, card_moves) can resolve destinations including any
	// decks claude is creating in this same plan. Per-deck scopes
	// hit none of these blocks; the map is built but unused.
	deckNameToID, err := buildDeckNameToID(tx, in.UserID)
	if err != nil {
		return shared.TransformResult{}, fmt.Errorf("load deck names: %w", err)
	}

	// 1. Create proposed new decks first so subsequent ops can target
	//    them by name. SRS decks just need a name + optional context;
	//    trivia decks need an interval (default 30 if claude omits).
	for _, nd := range in.Plan.NewDecks {
		name := strings.TrimSpace(nd.Name)
		if name == "" {
			continue
		}
		if _, exists := deckNameToID[name]; exists {
			// Don't clobber an existing deck — would surprise the user
			// and complicate the rollback story. Skip silently.
			continue
		}
		var deckID int64
		switch nd.DeckType {
		case "trivia":
			interval := nd.IntervalMinutes
			if interval <= 0 {
				interval = 30
			}
			r, err := tx.Exec(`
				INSERT INTO decks (user_id, name, created_at, context_prompt,
				                   deck_type, notification_interval_minutes)
				VALUES (?, ?, ?, ?, 'trivia', ?)`,
				in.UserID, name, nowISO(), nullIfEmpty(nd.Topic), interval,
			)
			if err != nil {
				return shared.TransformResult{}, fmt.Errorf("insert new trivia deck %q: %w", name, err)
			}
			deckID, _ = r.LastInsertId()
		default:
			r, err := tx.Exec(`
				INSERT INTO decks (user_id, name, created_at, context_prompt)
				VALUES (?, ?, ?, ?)`,
				in.UserID, name, nowISO(), nullIfEmpty(nd.Topic),
			)
			if err != nil {
				return shared.TransformResult{}, fmt.Errorf("insert new srs deck %q: %w", name, err)
			}
			deckID, _ = r.LastInsertId()
		}
		deckNameToID[name] = int(deckID)
		res.CreatedDeckIDs = append(res.CreatedDeckIDs, int(deckID))
	}

	// 2. Renames. Skip if the new name collides with an existing deck.
	for _, rn := range in.Plan.DeckRenames {
		newName := strings.TrimSpace(rn.NewName)
		if newName == "" {
			continue
		}
		if _, collides := deckNameToID[newName]; collides {
			continue
		}
		// Pull the old name BEFORE we update so we can adjust the map.
		var oldName string
		if err := tx.QueryRow(
			`SELECT name FROM decks WHERE id = ? AND user_id = ?`,
			rn.DeckID, in.UserID,
		).Scan(&oldName); err == sql.ErrNoRows {
			continue
		} else if err != nil {
			return shared.TransformResult{}, fmt.Errorf("read deck %d: %w", rn.DeckID, err)
		}
		if _, err := tx.Exec(
			`UPDATE decks SET name = ? WHERE id = ? AND user_id = ?`,
			newName, rn.DeckID, in.UserID,
		); err != nil {
			return shared.TransformResult{}, fmt.Errorf("rename deck %d: %w", rn.DeckID, err)
		}
		delete(deckNameToID, oldName)
		deckNameToID[newName] = rn.DeckID
		res.RenamedDeckIDs = append(res.RenamedDeckIDs, rn.DeckID)
	}

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
			       language = ?,
			       explanation = ?,
			       answer_regex = ?
			 WHERE id = ? AND user_id = ?`,
			m.Type, nullIfEmpty(m.Topic), m.Prompt, choicesJSON,
			m.Answer, nullIfEmpty(m.Rubric),
			nullIfEmpty(m.Skeleton), nullIfEmpty(m.Language),
			nullIfEmpty(m.Explanation), nullIfEmpty(m.AnswerRegex),
			m.QuestionID, in.UserID,
		)
		if err != nil {
			return shared.TransformResult{}, fmt.Errorf("update qid %d: %w", m.QuestionID, err)
		}
		res.ModifiedIDs = append(res.ModifiedIDs, m.QuestionID)
	}

	// ---- Additions ----
	// Per-addition deck resolution:
	// - For per-deck scopes, in.DeckID is set; addition.DestDeck is "".
	// - For reorganize scope, in.DeckID is 0; each addition specifies
	//   DestDeck (an existing deck name OR a NewDecks entry created in
	//   step 1). Skip silently when the name doesn't resolve — claude
	//   may have hallucinated.
	for _, a := range in.Plan.Additions {
		destID := in.DeckID
		if a.DestDeck != "" {
			id, ok := deckNameToID[a.DestDeck]
			if !ok {
				continue
			}
			destID = id
		}
		if destID == 0 {
			continue
		}
		// Look up the destination's type for the trivia_queue branch.
		var deckType string
		if err := tx.QueryRow(
			`SELECT COALESCE(deck_type, 'srs') FROM decks WHERE id = ? AND user_id = ?`,
			destID, in.UserID,
		).Scan(&deckType); err != nil {
			deckType = "srs"
		}
		choicesJSON := jsonOrNull(a.Choices)
		var skel, lang sql.NullString
		if a.Type == "code" {
			if a.Skeleton != "" {
				skel = sql.NullString{String: a.Skeleton, Valid: true}
			}
			if a.Language != "" {
				lang = sql.NullString{String: a.Language, Valid: true}
			}
		}
		r, err := tx.Exec(`
			INSERT INTO questions (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language, explanation, answer_regex)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			in.UserID, destID, a.Type, nullIfEmpty(a.Topic), a.Prompt, choicesJSON,
			a.Answer, nullIfEmpty(a.Rubric), nowISO(), skel, lang,
			nullIfEmpty(a.Explanation), nullIfEmpty(a.AnswerRegex),
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
		// Trivia decks need a trivia_queue row so the new card enters
		// the rotation. Position = max(queue_position) + 1 within deck.
		if deckType == "trivia" {
			var nextPos int
			if err := tx.QueryRow(`
				SELECT COALESCE(MAX(tq.queue_position), 0) + 1
				  FROM trivia_queue tq
				  JOIN questions q ON q.id = tq.question_id
				 WHERE q.deck_id = ?`, destID).Scan(&nextPos); err != nil {
				return shared.TransformResult{}, fmt.Errorf("compute trivia queue pos: %w", err)
			}
			if _, err := tx.Exec(`INSERT INTO trivia_queue (question_id, queue_position) VALUES (?, ?)`,
				cardID, nextPos); err != nil {
				return shared.TransformResult{}, fmt.Errorf("insert trivia_queue row: %w", err)
			}
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

	// ---- Card moves (cross-deck) ----
	// Each move reassigns a question to a different deck. Resolves
	// dest_deck_name → deck_id via the map built above (which already
	// includes any decks created in this same plan). Trivia_queue
	// invariant: a question must have a queue row IFF its (now-)owning
	// deck is trivia. After moving, sync that invariant.
	for _, mv := range in.Plan.CardMoves {
		destID, ok := deckNameToID[mv.DestDeck]
		if !ok || destID == 0 {
			continue
		}
		// Confirm ownership.
		var owner string
		var sourceDeckID int
		if err := tx.QueryRow(
			`SELECT user_id, deck_id FROM questions WHERE id = ?`, mv.QuestionID,
		).Scan(&owner, &sourceDeckID); err == sql.ErrNoRows {
			continue
		} else if err != nil {
			return shared.TransformResult{}, fmt.Errorf("read move target qid %d: %w", mv.QuestionID, err)
		}
		if owner != in.UserID || sourceDeckID == destID {
			continue
		}
		if _, err := tx.Exec(
			`UPDATE questions SET deck_id = ? WHERE id = ? AND user_id = ?`,
			destID, mv.QuestionID, in.UserID,
		); err != nil {
			return shared.TransformResult{}, fmt.Errorf("move qid %d: %w", mv.QuestionID, err)
		}
		// Trivia_queue invariant maintenance.
		var destType string
		if err := tx.QueryRow(
			`SELECT COALESCE(deck_type, 'srs') FROM decks WHERE id = ?`, destID,
		).Scan(&destType); err != nil {
			return shared.TransformResult{}, fmt.Errorf("read dest deck type: %w", err)
		}
		var hasQueueRow bool
		_ = tx.QueryRow(
			`SELECT 1 FROM trivia_queue WHERE question_id = ?`, mv.QuestionID,
		).Scan(&hasQueueRow)
		if destType == "trivia" && !hasQueueRow {
			var nextPos int
			if err := tx.QueryRow(`
				SELECT COALESCE(MAX(tq.queue_position), 0) + 1
				  FROM trivia_queue tq
				  JOIN questions q ON q.id = tq.question_id
				 WHERE q.deck_id = ?`, destID).Scan(&nextPos); err != nil {
				return shared.TransformResult{}, fmt.Errorf("compute trivia queue pos for moved qid %d: %w", mv.QuestionID, err)
			}
			if _, err := tx.Exec(
				`INSERT INTO trivia_queue (question_id, queue_position) VALUES (?, ?)`,
				mv.QuestionID, nextPos,
			); err != nil {
				return shared.TransformResult{}, fmt.Errorf("insert trivia_queue for moved qid %d: %w", mv.QuestionID, err)
			}
		} else if destType != "trivia" && hasQueueRow {
			if _, err := tx.Exec(
				`DELETE FROM trivia_queue WHERE question_id = ?`, mv.QuestionID,
			); err != nil {
				return shared.TransformResult{}, fmt.Errorf("delete trivia_queue for moved qid %d: %w", mv.QuestionID, err)
			}
		}
		res.MovedCardIDs = append(res.MovedCardIDs, mv.QuestionID)
	}

	// ---- Deck deletions ----
	// FK CASCADE wipes the deck's questions/cards/reviews/queue rows.
	// Active trivia sessions on the deck cascade as well.
	for _, did := range in.Plan.DeckDeletions {
		r, err := tx.Exec(`DELETE FROM decks WHERE id = ? AND user_id = ?`, did, in.UserID)
		if err != nil {
			return shared.TransformResult{}, fmt.Errorf("delete deck %d: %w", did, err)
		}
		if n, _ := r.RowsAffected(); n > 0 {
			res.DeletedDeckIDs = append(res.DeletedDeckIDs, did)
		}
	}

	if err := tx.Commit(); err != nil {
		return shared.TransformResult{}, err
	}
	return res, nil
}

// buildDeckNameToID returns name → deck_id for all of the user's
// decks. Used by the apply path so cross-deck operations (additions
// with dest_deck, card_moves) can resolve names → ids — including
// names of decks claude is creating in the same plan.
func buildDeckNameToID(tx *sql.Tx, userID string) (map[string]int, error) {
	rows, err := tx.Query(`SELECT id, name FROM decks WHERE user_id = ?`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var id int
		var name string
		if err := rows.Scan(&id, &name); err != nil {
			return nil, err
		}
		out[name] = id
	}
	return out, rows.Err()
}

// deckForTransform is one deck + its cards, sent to claude during a
// reorganize compute. JSON-marshaled into the prompt so claude can
// reason across the user's whole library.
type deckForTransform struct {
	ID              int                `json:"id"`
	Name            string             `json:"name"`
	DeckType        string             `json:"deck_type"`
	Topic           string             `json:"topic,omitempty"`
	IntervalMinutes int                `json:"interval_minutes,omitempty"`
	Cards           []cardForTransform `json:"cards"`
}

func loadAllUserDecksForTransform(dbPath, userID string) ([]deckForTransform, error) {
	db, err := openDB(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	deckRows, err := db.Query(`
		SELECT id, name, COALESCE(deck_type, 'srs'),
		       COALESCE(context_prompt, ''),
		       COALESCE(notification_interval_minutes, 0)
		  FROM decks WHERE user_id = ? ORDER BY name`, userID)
	if err != nil {
		return nil, err
	}
	defer deckRows.Close()
	var out []deckForTransform
	for deckRows.Next() {
		var d deckForTransform
		if err := deckRows.Scan(&d.ID, &d.Name, &d.DeckType, &d.Topic, &d.IntervalMinutes); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	if err := deckRows.Err(); err != nil {
		return nil, err
	}
	// Now load cards per deck. Cheap to batch by deck since N is small.
	for i := range out {
		cards, err := loadDeckCardsForTransform(dbPath, userID, out[i].ID)
		if err != nil {
			return nil, fmt.Errorf("load cards for deck %d: %w", out[i].ID, err)
		}
		out[i].Cards = cards
		if out[i].Cards == nil {
			out[i].Cards = []cardForTransform{}
		}
	}
	return out, nil
}

func reorganizeScopePrompt(decks []deckForTransform, userPrompt string) string {
	if decks == nil {
		decks = []deckForTransform{}
	}
	decksJSON, _ := json.MarshalIndent(decks, "", "  ")
	return fmt.Sprintf(`You are restructuring a user's flashcard library across multiple decks, per their request.

**Current decks (JSON, with cards):**
`+"```json"+`
%s
`+"```"+`

**User's request:**
%s

You can:
- Edit cards (modifications) — change prompt/answer/explanation/etc on any existing card.
- Add cards (additions) — each addition specifies dest_deck (the destination deck name; existing OR a name you propose in new_decks).
- Delete cards (deletions) — by question_id.
- Create new decks (new_decks) — name + deck_type ("srs" or "trivia") + topic + interval_minutes (trivia only; default 30).
- Move cards between decks (card_moves) — each move references question_id + dest_deck (name).
- Rename existing decks (deck_renames) — by deck_id + new_name.
- Delete decks (deck_deletions) — by deck_id; cascades through the deck's cards.

Do ONLY what the user's request implies. If the request is "fix typos across all decks", return modifications, no new_decks/moves. If the request is "split deck X into Y and Z", return new_decks for Y/Z and card_moves placing each existing card in its new home; NO modifications unless the user also asked for content edits.

For trivia cards: when you change prompt or answer, also update explanation + answer_regex so they stay in sync. answer_regex is a case-insensitive Python regex (re.fullmatch) that should match the new answer + obvious legitimate alternative forms.

If URLs or recent material are referenced, you may use your web-fetch / web-search tools to ground the change.

Return ONLY a JSON object, no commentary or fences. Shape:

`+"```json"+`
{
  "modifications": [
    {"question_id": <id>, "type": "...", "topic": "...", "prompt": "...", "choices": [...], "answer": "...", "rubric": "...", "skeleton": "...", "language": "...", "explanation": "...", "answer_regex": "..."}
  ],
  "additions": [
    {"dest_deck": "<deck name>", "type": "code|mcq|multi|short", "topic": "...", "prompt": "...", "answer": "...", ...}
  ],
  "deletions": [<question_id>, ...],
  "new_decks": [
    {"name": "...", "deck_type": "srs|trivia", "topic": "...", "interval_minutes": 30}
  ],
  "card_moves": [
    {"question_id": <id>, "dest_deck": "<deck name>"}
  ],
  "deck_renames": [
    {"deck_id": <id>, "new_name": "..."}
  ],
  "deck_deletions": [<deck_id>, ...],
  "notes": "<one short sentence summarizing what changed>"
}
`+"```"+`

Cap additions at 25 per request. If the request implies fewer than one change, return empty arrays. Do not invent operations beyond what the request asks for.`,
		string(decksJSON), userPrompt)
}

// ---- helpers -----------------------------------------------------------

// cardForTransform is the shape we pass to claude — a subset of the row
// that's relevant to rewriting. Excludes srs state, timestamps, and ids
// the model doesn't need to think about.
type cardForTransform struct {
	QuestionID  int      `json:"question_id"`
	Type        string   `json:"type"`
	Topic       string   `json:"topic,omitempty"`
	Prompt      string   `json:"prompt"`
	Choices     []string `json:"choices,omitempty"`
	Answer      string   `json:"answer"`
	Rubric      string   `json:"rubric,omitempty"`
	Skeleton    string   `json:"skeleton,omitempty"`
	Language    string   `json:"language,omitempty"`
	Explanation string   `json:"explanation,omitempty"`
	AnswerRegex string   `json:"answer_regex,omitempty"`
}

func loadCardForTransform(dbPath, userID string, qid int) ([]cardForTransform, error) {
	db, err := openDB(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	row := db.QueryRow(`
		SELECT id, type, COALESCE(topic, ''), prompt, choices, answer,
		       COALESCE(rubric, ''), COALESCE(skeleton, ''), COALESCE(language, ''),
		       COALESCE(explanation, ''), COALESCE(answer_regex, '')
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
		       COALESCE(rubric, ''), COALESCE(skeleton, ''), COALESCE(language, ''),
		       COALESCE(explanation, ''), COALESCE(answer_regex, '')
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
		&choicesJSON, &c.Answer, &c.Rubric, &c.Skeleton, &c.Language,
		&c.Explanation, &c.AnswerRegex); err != nil {
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
