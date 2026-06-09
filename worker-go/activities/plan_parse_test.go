package activities

import (
	"testing"
)

// Each case is one pattern we've seen from claude on the plan-generate
// path. End-to-end test of parsePlanJSON — the contract is "claude
// returned this body; we extract a non-empty PlanItem slice."
func TestParsePlanJSONHandlesObservedShapes(t *testing.T) {
	tests := []struct {
		name string
		in   string
	}{
		{
			name: "raw array",
			in:   `[{"title":"x","brief":"y","type":"short"}]`,
		},
		{
			name: "wrapper object",
			in:   `{"plan":[{"title":"x","brief":"y","type":"short"}]}`,
		},
		{
			name: "fenced at start with json tag",
			in:   "```json\n[{\"title\":\"x\",\"brief\":\"y\",\"type\":\"short\"}]\n```",
		},
		{
			name: "preamble + fenced block (a real prod failure mode)",
			in: "Here is the new 63-card plan, expanded to cover every item in your outline: ```json\n" +
				`[{"title":"List: add ops","brief":"What do append, extend, and insert do?","type":"short"}]` +
				"\n```",
		},
		{
			name: "preamble + no fence",
			in:   `Here is your plan: [{"title":"x","brief":"y","type":"short"}] Hope that helps!`,
		},
		{
			name: "fence with no closing fence (truncated stream)",
			in:   "```json\n[{\"title\":\"x\",\"brief\":\"y\",\"type\":\"short\"}]",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parsePlanJSON([]byte(tc.in))
			if err != nil {
				t.Fatalf("parsePlanJSON: %v", err)
			}
			if len(got) == 0 {
				t.Fatalf("parsePlanJSON: empty plan")
			}
			if got[0].Title == "" {
				t.Errorf("first item missing title: %+v", got[0])
			}
		})
	}
}
