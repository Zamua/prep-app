// agent-server — host-side wrapper around the claude CLI that exposes
// a small HTTP contract the prep worker (in a container) can call
// without needing claude on the worker's PATH or in its keychain.
//
// Endpoints:
//   POST /run         — invoke `claude -p` with the prompt, return stdout
//   GET  /healthz     — claude auth status; tells the prep app whether
//                       AI features should light up
//   POST /connect     — accept a CLAUDE_CODE_OAUTH_TOKEN (the user got
//                       it from `claude setup-token` on any machine
//                       they own) and persist it in our volume
//   POST /disconnect  — wipe the token; back to "not connected"
//
// Auth model: anthropic explicitly forbids embedding the subscription
// OAuth flow in third-party apps (their feb 2026 policy update). We
// don't try. Instead we accept a long-lived token the user generates
// themselves via `claude setup-token` and inject it as the
// `CLAUDE_CODE_OAUTH_TOKEN` env var on every claude invocation. No
// keychain access needed, no TTY wrapping.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	defaultPort       = "9999"
	defaultClaudeBin  = "claude"
	defaultArgsCSV    = "--strict-mcp-config,--mcp-config,{mcp_config},-p"
	emptyMCPConfig    = `{"mcpServers":{}}`
	defaultTokenPath  = "/data/agent-token"
	tokenFilePerm     = 0o600
	healthzCmdTimeout = 8 * time.Second
)

type Server struct {
	mu         sync.RWMutex
	token      string
	tokenPath  string
	claudeBin  string
	argsCSV    string
}

type runReq struct {
	Prompt    string `json:"prompt"`
	SessionID string `json:"session_id,omitempty"`
	ResumeID  string `json:"resume_id,omitempty"`
}

type runResp struct {
	Stdout string `json:"stdout"`
}

type errResp struct {
	Error string `json:"error"`
}

type healthResp struct {
	Ok               bool   `json:"ok"`
	LoggedIn         bool   `json:"logged_in"`
	Email            string `json:"email,omitempty"`
	OrgName          string `json:"org_name,omitempty"`
	SubscriptionType string `json:"subscription_type,omitempty"`
	Reason           string `json:"reason,omitempty"`
}

type connectReq struct {
	Token string `json:"token"`
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lshortfile)
	s := &Server{
		tokenPath: envOr("PREP_AGENT_TOKEN_PATH", defaultTokenPath),
		claudeBin: envOr("PREP_AGENT_BIN", defaultClaudeBin),
		argsCSV:   envOr("PREP_AGENT_ARGS", defaultArgsCSV),
	}
	if err := s.loadToken(); err != nil {
		log.Printf("token load: %v (continuing — user will need to /connect)", err)
	}
	if s.token != "" {
		log.Println("token loaded from disk; AI invocations will be authenticated")
	} else {
		log.Println("no token yet; /run will fail until POST /connect lands a token")
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/run", s.handleRun)
	mux.HandleFunc("/connect", s.handleConnect)
	mux.HandleFunc("/disconnect", s.handleDisconnect)

	addr := ":" + envOr("PREP_AGENT_PORT", defaultPort)
	log.Printf("agent-server listening on %s (claude=%s)", addr, s.claudeBin)
	log.Fatal(http.ListenAndServe(addr, mux))
}

// ---- token persistence ----------------------------------------------

func (s *Server) loadToken() error {
	data, err := os.ReadFile(s.tokenPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	s.token = strings.TrimSpace(string(data))
	return nil
}

func (s *Server) saveToken(tok string) error {
	if err := os.MkdirAll(filepath.Dir(s.tokenPath), 0o700); err != nil {
		return fmt.Errorf("mkdir token dir: %w", err)
	}
	if err := os.WriteFile(s.tokenPath, []byte(tok), tokenFilePerm); err != nil {
		return fmt.Errorf("write token: %w", err)
	}
	return nil
}

func (s *Server) wipeToken() error {
	if err := os.Remove(s.tokenPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// ---- /healthz -------------------------------------------------------

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	s.mu.RLock()
	tok := s.token
	s.mu.RUnlock()

	resp := healthResp{Ok: true}
	if tok == "" {
		resp.LoggedIn = false
		resp.Reason = "no token; POST /connect with a CLAUDE_CODE_OAUTH_TOKEN"
		writeJSON(w, http.StatusOK, resp)
		return
	}

	// We have a token — sanity-check it by asking claude. The CLI reads
	// CLAUDE_CODE_OAUTH_TOKEN from env and uses it for the call.
	cmd := exec.Command(s.claudeBin, "auth", "status", "--json")
	cmd.Env = append(os.Environ(), "CLAUDE_CODE_OAUTH_TOKEN="+tok)
	out, err := runWithTimeout(cmd, healthzCmdTimeout)
	if err != nil {
		resp.LoggedIn = false
		resp.Reason = fmt.Sprintf("claude auth status failed: %v", err)
		writeJSON(w, http.StatusOK, resp)
		return
	}
	var status struct {
		LoggedIn         bool   `json:"loggedIn"`
		AuthMethod       string `json:"authMethod"`
		Email            string `json:"email"`
		OrgName          string `json:"orgName"`
		SubscriptionType string `json:"subscriptionType"`
	}
	if err := json.Unmarshal(out, &status); err != nil {
		resp.LoggedIn = false
		resp.Reason = fmt.Sprintf("claude auth status returned non-JSON: %s", truncate(string(out), 200))
		writeJSON(w, http.StatusOK, resp)
		return
	}
	resp.LoggedIn = status.LoggedIn
	resp.Email = status.Email
	resp.OrgName = status.OrgName
	resp.SubscriptionType = status.SubscriptionType
	if !status.LoggedIn {
		resp.Reason = "token invalid or expired — POST /connect with a fresh token"
	}
	writeJSON(w, http.StatusOK, resp)
}

// ---- /run -----------------------------------------------------------

func (s *Server) handleRun(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req runReq
	if err := json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errResp{Error: fmt.Sprintf("decode: %v", err)})
		return
	}
	if req.Prompt == "" {
		writeJSON(w, http.StatusBadRequest, errResp{Error: "prompt is required"})
		return
	}
	if req.SessionID != "" && req.ResumeID != "" {
		writeJSON(w, http.StatusBadRequest, errResp{Error: "session_id and resume_id are mutually exclusive"})
		return
	}

	s.mu.RLock()
	tok := s.token
	s.mu.RUnlock()
	if tok == "" {
		writeJSON(w, http.StatusServiceUnavailable, errResp{Error: "agent not connected — POST /connect first"})
		return
	}

	// Build argv: [--session-id <id> | --resume <id>]? <argsCSV expanded> <prompt>
	args := []string{}
	switch {
	case req.SessionID != "":
		args = append(args, "--session-id", req.SessionID)
	case req.ResumeID != "":
		args = append(args, "--resume", req.ResumeID)
	}
	args = append(args, expandArgs(s.argsCSV)...)
	args = append(args, req.Prompt)

	cmd := exec.CommandContext(r.Context(), s.claudeBin, args...)
	cmd.Env = append(os.Environ(), "CLAUDE_CODE_OAUTH_TOKEN="+tok)
	out, err := cmd.CombinedOutput()
	if err != nil {
		writeJSON(w, http.StatusBadGateway, errResp{
			Error: fmt.Sprintf("claude invocation failed: %v (output: %s)", err, truncate(string(out), 800)),
		})
		return
	}
	writeJSON(w, http.StatusOK, runResp{Stdout: string(out)})
}

// ---- /connect, /disconnect -----------------------------------------

func (s *Server) handleConnect(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req connectReq
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errResp{Error: fmt.Sprintf("decode: %v", err)})
		return
	}
	tok := strings.TrimSpace(req.Token)
	if tok == "" {
		writeJSON(w, http.StatusBadRequest, errResp{Error: "token is required"})
		return
	}
	// We deliberately don't try to validate the token against anthropic
	// at connect time — `claude auth status --json` reports loggedIn=true
	// for any token-shaped string the env var holds (it's structural,
	// not live), and a real validation call would burn the user's quota
	// just to ack a connection. We save optimistically; the first /run
	// surfaces a bad/expired token via claude's own error path and the
	// UI can prompt for a reconnect.
	//
	// We do reject the most common paste mistake — the OAuth URL — so
	// that fat-fingering doesn't silently store garbage. Beyond that we
	// trust the user's paste; token prefixes vary across account types
	// (sk-ant-oat… for the documented setup-token output, but other
	// shapes have been seen in the wild) and we'd rather accept something
	// that fails on /run than reject something that would have worked.
	if strings.HasPrefix(tok, "http://") || strings.HasPrefix(tok, "https://") {
		writeJSON(w, http.StatusBadRequest, errResp{
			Error: "that looks like a URL, not a token. paste the token (a long string, not the verification URL) printed by `claude setup-token` after you finished the OAuth flow.",
		})
		return
	}
	if len(tok) < 20 {
		writeJSON(w, http.StatusBadRequest, errResp{
			Error: "that's too short to be a valid token. did the paste get truncated?",
		})
		return
	}

	if err := s.saveToken(tok); err != nil {
		writeJSON(w, http.StatusInternalServerError, errResp{Error: err.Error()})
		return
	}
	s.mu.Lock()
	s.token = tok
	s.mu.Unlock()
	log.Println("connected — token saved")
	writeJSON(w, http.StatusOK, healthResp{Ok: true, LoggedIn: true})
}

func (s *Server) handleDisconnect(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if err := s.wipeToken(); err != nil {
		writeJSON(w, http.StatusInternalServerError, errResp{Error: err.Error()})
		return
	}
	s.mu.Lock()
	s.token = ""
	s.mu.Unlock()
	log.Println("disconnected — token wiped")
	writeJSON(w, http.StatusOK, healthResp{Ok: true, LoggedIn: false})
}

// ---- helpers --------------------------------------------------------

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func expandArgs(csv string) []string {
	out := []string{}
	for _, a := range strings.Split(csv, ",") {
		a = strings.TrimSpace(a)
		if a == "" {
			continue
		}
		a = strings.ReplaceAll(a, "{mcp_config}", emptyMCPConfig)
		out = append(out, a)
	}
	return out
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("content-type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func runWithTimeout(cmd *exec.Cmd, timeout time.Duration) ([]byte, error) {
	if cmd.Stdout != nil || cmd.Stderr != nil {
		return nil, fmt.Errorf("runWithTimeout: cmd already has stdout/stderr wired")
	}
	done := make(chan struct{})
	var out []byte
	var runErr error
	go func() {
		out, runErr = cmd.CombinedOutput()
		close(done)
	}()
	select {
	case <-done:
		return out, runErr
	case <-time.After(timeout):
		_ = cmd.Process.Kill()
		<-done
		return out, fmt.Errorf("timeout after %s", timeout)
	}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
