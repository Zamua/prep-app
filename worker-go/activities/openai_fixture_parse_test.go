package activities

// Table tests over the recorded OpenAI-compat response fixtures shared
// with the Python suite (tests/agent/fixtures/openai_compat/). The Go
// parse helpers must cope with the same shapes, pinning the documented
// fallback (error out, coerce to "wrong", backfill) rather than silent
// bad data.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type fixtureEnvelope struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
}

func fixtureContent(t *testing.T, name string) string {
	t.Helper()
	path := filepath.Join("..", "..", "tests", "agent", "fixtures", "openai_compat", name)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	var env fixtureEnvelope
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("unmarshal fixture %s: %v", name, err)
	}
	if len(env.Choices) == 0 {
		t.Fatalf("fixture %s has no choices", name)
	}
	return env.Choices[0].Message.Content
}

func TestParseTriviaJSONFixtures(t *testing.T) {
	tests := []struct {
		fixture   string
		wantPairs int
		wantErr   bool
	}{
		{fixture: "happy.json", wantPairs: 3},
		{fixture: "preamble.json", wantPairs: 2},
		{fixture: "truncated.json", wantErr: true},
		{fixture: "empty_content.json", wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.fixture, func(t *testing.T) {
			pairs, err := parseTriviaJSON(fixtureContent(t, tc.fixture))
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got %d pairs", len(pairs))
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if len(pairs) != tc.wantPairs {
				t.Fatalf("got %d pairs, want %d", len(pairs), tc.wantPairs)
			}
			for i, p := range pairs {
				if p.Q == "" || p.A == "" {
					t.Fatalf("pair %d has empty q/a: %+v", i, p)
				}
			}
		})
	}
}

func TestExtractJSONFixtures(t *testing.T) {
	// Fence-stripping and preamble trimming must yield a valid JSON
	// payload for the shapes the plan path can meet. think_tag is
	// excluded on purpose: extractJSON prefers '[' (plans are arrays)
	// and that shape's bracket lives inside a string value, so the
	// slice is garbage - parsePlanJSON then errors loudly (see below),
	// which is the documented activity-error fallback.
	for _, fixture := range []string{"happy.json", "fenced.json", "preamble.json"} {
		t.Run(fixture, func(t *testing.T) {
			out := extractJSON(fixtureContent(t, fixture))
			if !json.Valid([]byte(out)) {
				t.Fatalf("extractJSON did not yield valid JSON: %q", out)
			}
		})
	}
}

func TestParsePlanJSONFixturesDamagedShapesError(t *testing.T) {
	// Shapes that are not a plan must surface as an activity error,
	// never a silent empty/garbage plan.
	for _, fixture := range []string{"think_tag.json", "truncated.json", "empty_content.json"} {
		t.Run(fixture, func(t *testing.T) {
			if _, err := parsePlanJSON([]byte(fixtureContent(t, fixture))); err == nil {
				t.Fatal("expected error for non-plan shape")
			}
		})
	}
}

func TestParseVerdictJSONFixtures(t *testing.T) {
	// fenced + think_tag carry a grade object in the Python trivia
	// shape ({"verdict": ...}), not this activity's {"result": ...}.
	// The pin is the fail-safe direction: an unknown verdict coerces
	// to "wrong" and the model answer is backfilled - never a silent
	// pass, never a crash on fences or reasoning preambles.
	for _, fixture := range []string{"fenced.json", "think_tag.json"} {
		t.Run(fixture, func(t *testing.T) {
			v, err := parseVerdictJSON([]byte(fixtureContent(t, fixture)), "write-ahead log")
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if v.Result != "wrong" {
				t.Fatalf("unknown verdict must coerce to wrong, got %q", v.Result)
			}
			if v.ModelAnswerSummary != "write-ahead log" {
				t.Fatalf("model answer not backfilled: %q", v.ModelAnswerSummary)
			}
		})
	}
	for _, fixture := range []string{"truncated.json", "empty_content.json"} {
		t.Run(fixture, func(t *testing.T) {
			if _, err := parseVerdictJSON([]byte(fixtureContent(t, fixture)), "x"); err == nil {
				t.Fatal("expected error for damaged shape")
			}
		})
	}
}

func TestParseCardJSONFixtures(t *testing.T) {
	// None of the recorded shapes is a valid Card; every one must
	// surface as an activity error, never a zero-value card.
	for _, fixture := range []string{"happy.json", "fenced.json", "truncated.json", "empty_content.json"} {
		t.Run(fixture, func(t *testing.T) {
			if _, err := parseCardJSON([]byte(fixtureContent(t, fixture))); err == nil {
				t.Fatal("expected error, got a card")
			}
		})
	}
}
