package activities

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"prep-worker/shared"
)

func triviaJSON(n int) string {
	items := make([]string, 0, n)
	for i := range n {
		items = append(items, fmt.Sprintf(`{"q":"q%d","a":"a%d","e":"e%d"}`, i, i, i))
	}
	return "[" + strings.Join(items, ",") + "]"
}

// emptyQuestionsDB satisfies GenerateTriviaBatch's existing-prompts read.
func emptyQuestionsDB(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.sqlite")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()
	if _, err := db.Exec(`CREATE TABLE questions (id INTEGER PRIMARY KEY, deck_id INTEGER, prompt TEXT)`); err != nil {
		t.Fatalf("create table: %v", err)
	}
	return path
}

// BatchSize is enforced in code: an overshooting batch is truncated, not trusted.
func TestGenerateTriviaBatchEnforcesBatchSize(t *testing.T) {
	a := &Activities{Cfg: &Config{DBPath: emptyQuestionsDB(t), Agent: stubAgent{stdout: triviaJSON(8)}}}
	pairs, err := a.GenerateTriviaBatch(context.Background(), shared.GenerateTriviaInput{
		UserID: "u", DeckID: 1, Topic: "t", BatchSize: 5,
	})
	if err != nil {
		t.Fatalf("GenerateTriviaBatch: %v", err)
	}
	if len(pairs) != 5 {
		t.Fatalf("want 5 pairs, got %d", len(pairs))
	}
}

// BatchSize == 0 (default sizing) leaves the parsed batch untruncated.
func TestGenerateTriviaBatchUntruncatedWhenBatchSizeZero(t *testing.T) {
	a := &Activities{Cfg: &Config{DBPath: emptyQuestionsDB(t), Agent: stubAgent{stdout: triviaJSON(8)}}}
	pairs, err := a.GenerateTriviaBatch(context.Background(), shared.GenerateTriviaInput{
		UserID: "u", DeckID: 1, Topic: "t",
	})
	if err != nil {
		t.Fatalf("GenerateTriviaBatch: %v", err)
	}
	if len(pairs) != 8 {
		t.Fatalf("want 8 pairs, got %d", len(pairs))
	}
}
