package activities

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"prep-worker/shared"
)

// ---- SQLite ------------------------------------------------------------

// openDB opens the prep-app's SQLite in WAL mode so the Go worker and the
// FastAPI app (Python) can read/write concurrently without locking each
// other out.
func openDB(path string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	// WAL + busy timeout — defensive against concurrent writers.
	if _, err := db.Exec(`
		PRAGMA journal_mode=WAL;
		PRAGMA busy_timeout=5000;
		PRAGMA foreign_keys=ON;
	`); err != nil {
		db.Close()
		return nil, err
	}
	// Ensure the idempotency column exists. (Go worker is the source of
	// truth for this column; Python doesn't need to know about it.)
	if _, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS questions_idempotency (
			idempotency_key TEXT PRIMARY KEY,
			question_id     INTEGER NOT NULL,
			created_at      TEXT NOT NULL
		);
	`); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// getCardByIdempotencyKey checks if a card with this key already exists
// (the at-least-once delivery guard — used by plan.go's
// GenerateCardFromBrief before paying for a fresh claude call).
func getCardByIdempotencyKey(dbPath, key string) (shared.Card, bool, error) {
	db, err := openDB(dbPath)
	if err != nil {
		return shared.Card{}, false, err
	}
	defer db.Close()

	var qid int
	err = db.QueryRow(`
		SELECT question_id FROM questions_idempotency WHERE idempotency_key = ?`, key).Scan(&qid)
	if errors.Is(err, sql.ErrNoRows) {
		return shared.Card{}, false, nil
	}
	if err != nil {
		return shared.Card{}, false, err
	}

	var c shared.Card
	var choicesJSON sql.NullString
	err = db.QueryRow(`
		SELECT type, COALESCE(topic,''), prompt, choices, answer, COALESCE(rubric,'')
		  FROM questions WHERE id = ?`, qid).Scan(
		&c.Type, &c.Topic, &c.Prompt, &choicesJSON, &c.Answer, &c.Rubric)
	if err != nil {
		return shared.Card{}, false, err
	}
	if choicesJSON.Valid && choicesJSON.String != "" {
		_ = json.Unmarshal([]byte(choicesJSON.String), &c.Choices)
	}
	return c, true, nil
}

// insertCard writes a card + records its idempotency key in one transaction.
// If the key already exists, we're a re-delivery — return the existing id.
func insertCard(dbPath string, in shared.InsertInput) (shared.InsertResult, error) {
	db, err := openDB(dbPath)
	if err != nil {
		return shared.InsertResult{}, err
	}
	defer db.Close()

	tx, err := db.Begin()
	if err != nil {
		return shared.InsertResult{}, err
	}
	defer tx.Rollback()

	// Dedup by key first.
	var existingID int
	err = tx.QueryRow(`SELECT question_id FROM questions_idempotency WHERE idempotency_key=?`,
		in.IdempotencyKey).Scan(&existingID)
	if err == nil {
		return shared.InsertResult{CardID: existingID, Duplicate: true}, tx.Commit()
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return shared.InsertResult{}, fmt.Errorf("idempotency check: %w", err)
	}

	// Look up deck id (creating if missing — mirrors the Python helper).
	// Scoped to user_id so two users with a deck named "cherry" each get
	// their own deck row.
	var deckID int
	err = tx.QueryRow(`SELECT id FROM decks WHERE name=? AND user_id=?`,
		in.DeckName, in.UserID).Scan(&deckID)
	if errors.Is(err, sql.ErrNoRows) {
		res, err := tx.Exec(`INSERT INTO decks (user_id, name, created_at) VALUES (?, ?, ?)`,
			in.UserID, in.DeckName, nowISO())
		if err != nil {
			return shared.InsertResult{}, err
		}
		id64, _ := res.LastInsertId()
		deckID = int(id64)
	} else if err != nil {
		return shared.InsertResult{}, err
	}

	// Encode choices to JSON if present.
	var choicesJSON sql.NullString
	if len(in.Card.Choices) > 0 {
		data, _ := json.Marshal(in.Card.Choices)
		choicesJSON = sql.NullString{String: string(data), Valid: true}
	}

	// Skeleton + language are only meaningful for code questions.
	var skeletonCol, languageCol sql.NullString
	if in.Card.Type == "code" {
		if in.Card.Skeleton != "" {
			skeletonCol = sql.NullString{String: in.Card.Skeleton, Valid: true}
		}
		// Default to "go" for code questions if the model didn't specify —
		// most of our cards are Go and an unknown language just falls back
		// to no highlighting on the client.
		lang := in.Card.Language
		if lang == "" {
			lang = "go"
		}
		languageCol = sql.NullString{String: lang, Valid: true}
	}

	res, err := tx.Exec(`
		INSERT INTO questions (user_id, deck_id, type, topic, prompt, choices, answer, rubric, created_at, skeleton, language)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		in.UserID, deckID, in.Card.Type, nullable(in.Card.Topic), in.Card.Prompt,
		choicesJSON, in.Card.Answer, nullable(in.Card.Rubric), nowISO(), skeletonCol, languageCol)
	if err != nil {
		return shared.InsertResult{}, fmt.Errorf("insert questions: %w", err)
	}
	id64, _ := res.LastInsertId()
	cardID := int(id64)

	// Card row in SRS schedule (matches what the Python add_question does).
	if _, err := tx.Exec(`
		INSERT INTO cards (question_id, step, next_due) VALUES (?, 0, ?)`,
		cardID, nowISO()); err != nil {
		return shared.InsertResult{}, fmt.Errorf("insert cards: %w", err)
	}

	if _, err := tx.Exec(`
		INSERT INTO questions_idempotency (idempotency_key, question_id, created_at)
		VALUES (?, ?, ?)`,
		in.IdempotencyKey, cardID, nowISO()); err != nil {
		return shared.InsertResult{}, fmt.Errorf("insert idempotency: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return shared.InsertResult{}, err
	}
	return shared.InsertResult{CardID: cardID, Duplicate: false}, nil
}

func nullable(s string) sql.NullString {
	if s == "" {
		return sql.NullString{}
	}
	return sql.NullString{String: s, Valid: true}
}

func nowISO() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}
