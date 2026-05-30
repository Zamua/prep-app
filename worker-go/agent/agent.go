// Package agent is the seam between the worker and an LLM-backed
// "agent" that generates / grades / transforms text. Two implementations
// ship in-tree:
//
//   - ShellAgent: exec.Command on a local CLI binary (claude, opencode,
//     aider, …). The original integration; keeps host-keychain auth.
//
//   - HTTPAgent: POST to a small HTTP server that wraps a CLI on the
//     host. Lets the worker run inside a container while the agent
//     binary stays on the host where it has credentials.
//
// The interface intentionally mirrors the CLI surface: a prompt + an
// optional session-id (to prime a session) or resume-id (to resume one).
// We do NOT abstract this into "messages with roles" or model
// parameters — that would lock us to a specific provider's vocabulary
// and bloat the contract. Sessions are an opaque pass-through so the
// underlying CLI / server decides what to do with them.
package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"
)

// Client is the worker's window onto an agent. All AI-backed work in
// the worker funnels through Run().
type Client interface {
	Run(ctx context.Context, in RunInput) (RunOutput, error)
}

// RunInput describes a single agent invocation.
//
// SessionID and ResumeID are mutually exclusive. Set SessionID to mint
// a fresh session with that ID (so future ResumeID calls can pick up
// where this one left off — used by GenerateCards for prompt-cache
// reuse across per-card prompts). Set ResumeID to continue an
// already-primed session. Set neither for a one-shot invocation.
type RunInput struct {
	Prompt    string `json:"prompt"`
	SessionID string `json:"session_id,omitempty"`
	ResumeID  string `json:"resume_id,omitempty"`
}

// RunOutput is whatever the agent printed to stdout, byte-for-byte.
// Worker-side parsers (parseCardJSON, parseVerdictJSON, …) interpret it.
// We don't impose a JSON schema at this layer — the agent contract is
// "give me text," not "give me structured data."
type RunOutput struct {
	Stdout string `json:"stdout"`
}

// ---- ShellAgent ------------------------------------------------------

// ShellAgent shells out to a local CLI. Bin and Args are read from
// PREP_AGENT_BIN / PREP_AGENT_ARGS at boot.
//
// Args is a comma-separated template; {mcp_config} is replaced with an
// empty MCP config literal, so the default args
// ("--strict-mcp-config,--mcp-config,{mcp_config},-p") produce the
// claude-code invocation we want without dragging the user's plugin
// config into one-shot generation calls.
type ShellAgent struct {
	Bin  string
	Args string // CSV template
}

const emptyMCPConfig = `{"mcpServers":{}}`

const DefaultArgs = "--strict-mcp-config,--mcp-config,{mcp_config},-p"

func (a *ShellAgent) Run(ctx context.Context, in RunInput) (RunOutput, error) {
	if a.Bin == "" {
		return RunOutput{}, errors.New("ShellAgent.Bin is empty")
	}
	args := []string{}
	// Session-mode flags go BEFORE the configured Args template, so the
	// trailing -p / prompt position is preserved. Mutual exclusion is
	// enforced here so callers can't smuggle both through.
	switch {
	case in.SessionID != "" && in.ResumeID != "":
		return RunOutput{}, errors.New("SessionID and ResumeID are mutually exclusive")
	case in.SessionID != "":
		args = append(args, "--session-id", in.SessionID)
	case in.ResumeID != "":
		args = append(args, "--resume", in.ResumeID)
	}
	args = append(args, a.renderArgs()...)

	// Prompt rides on stdin instead of as the trailing argv element.
	// argv had a hard ARG_MAX (~128KB on Linux) that the cross-deck
	// reorganize prompt blows through ("argument list too long");
	// claude -p reads stdin when no positional prompt follows.
	cmd := exec.CommandContext(ctx, a.Bin, args...)
	cmd.Env = os.Environ()
	cmd.Stdin = strings.NewReader(in.Prompt)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return RunOutput{}, fmt.Errorf("agent shell failed: %w (output: %s)", err, truncate(string(out), 800))
	}
	return RunOutput{Stdout: string(out)}, nil
}

func (a *ShellAgent) renderArgs() []string {
	csv := a.Args
	if csv == "" {
		csv = DefaultArgs
	}
	out := []string{}
	for _, s := range strings.Split(csv, ",") {
		s = strings.TrimSpace(s)
		if s == "" {
			continue
		}
		s = strings.ReplaceAll(s, "{mcp_config}", emptyMCPConfig)
		out = append(out, s)
	}
	return out
}

// ---- HTTPAgent -------------------------------------------------------

// HTTPAgent posts to an HTTP server that wraps a Claude invocation.
//
// Today the only target is prep's own FastAPI handler at
// $PREP_AGENT_URL/run (typically http://localhost:8082/api/agent/run);
// the legacy agent-server container is gone post-SDK-migration. The
// wire format is preserved so older agent-server deploys still work
// without code changes.
//
// Wire format:
//
//	POST <BaseURL>/run
//	  headers:  Content-Type: application/json
//	            X-Internal-Token: <PREP_INTERNAL_TOKEN>   (sent if set)
//	  request:  { "prompt", "session_id"?, "resume_id"? }
//	  response: 200 { "stdout" }   |   non-2xx { "error" }
//
//	GET  <BaseURL>/healthz
//	  response: 200 { "ok": true, ... }
//
// InternalToken is the shared secret that prep's /api/agent/run
// requires in the X-Internal-Token header. Empty = don't send the
// header (back-compat for the legacy agent-server which had no auth).
type HTTPAgent struct {
	BaseURL       string
	InternalToken string
	Client        *http.Client // optional; defaults to a long-timeout client.
}

func (a *HTTPAgent) httpClient() *http.Client {
	if a.Client != nil {
		return a.Client
	}
	// Long ceiling on purpose. Claude calls inside the agent container
	// can run for many minutes (large-deck transforms, reorganize, plan
	// expansion against a context-heavy deck) and we'd rather surface a
	// clear "agent http: deadline exceeded" error than have temporal
	// guillotine the activity mid-flight. The activity-level
	// StartToCloseTimeout is set ABOVE this (31m) so the HTTP client
	// times out first with an attributable message, rather than the
	// workflow killing the activity with a generic timeout.
	return &http.Client{Timeout: 30 * time.Minute}
}

func (a *HTTPAgent) Run(ctx context.Context, in RunInput) (RunOutput, error) {
	if a.BaseURL == "" {
		return RunOutput{}, errors.New("HTTPAgent.BaseURL is empty")
	}
	body, err := json.Marshal(in)
	if err != nil {
		return RunOutput{}, fmt.Errorf("marshal request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		strings.TrimRight(a.BaseURL, "/")+"/run",
		bytes.NewReader(body))
	if err != nil {
		return RunOutput{}, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("content-type", "application/json")
	if a.InternalToken != "" {
		// prep's /api/agent/run rejects calls without this header
		// (fail-closed). Legacy agent-server ignored it harmlessly.
		req.Header.Set("X-Internal-Token", a.InternalToken)
	}

	resp, err := a.httpClient().Do(req)
	if err != nil {
		return RunOutput{}, fmt.Errorf("agent http call: %w", err)
	}
	defer resp.Body.Close()

	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20)) // 8 MiB cap
	if resp.StatusCode/100 != 2 {
		var errBody struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(raw, &errBody)
		msg := strings.TrimSpace(errBody.Error)
		if msg == "" {
			msg = truncate(string(raw), 400)
		}
		return RunOutput{}, fmt.Errorf("agent http %d: %s", resp.StatusCode, msg)
	}
	var out RunOutput
	if err := json.Unmarshal(raw, &out); err != nil {
		return RunOutput{}, fmt.Errorf("parse response: %w (raw: %s)", err, truncate(string(raw), 400))
	}
	return out, nil
}

// ---- Construction from env ------------------------------------------

// FromEnv returns a Client based on env vars. PREP_AGENT_URL takes
// precedence over PREP_AGENT_BIN (for the docker / agent-server case).
// Returns nil if neither is configured OR if PREP_AGENT_BIN points at
// a path that doesn't exist — keeps the Go worker's notion of "agent
// available" consistent with the Python probe in agent.py, so the UI
// (gated on the Python probe) never sends work the worker can't run.
func FromEnv() Client {
	if u := strings.TrimSpace(os.Getenv("PREP_AGENT_URL")); u != "" {
		return &HTTPAgent{
			BaseURL:       u,
			InternalToken: strings.TrimSpace(os.Getenv("PREP_INTERNAL_TOKEN")),
		}
	}
	bin := strings.TrimSpace(os.Getenv("PREP_AGENT_BIN"))
	if bin == "" {
		bin = strings.TrimSpace(os.Getenv("CLAUDE_BIN")) // back-compat alias
	}
	if bin == "" {
		return nil
	}
	if info, err := os.Stat(bin); err != nil || info.IsDir() {
		return nil
	}
	return &ShellAgent{
		Bin:  bin,
		Args: os.Getenv("PREP_AGENT_ARGS"),
	}
}

// ---- helpers --------------------------------------------------------

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
