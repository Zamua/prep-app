// prep-worker — Temporal worker for the prep-app.
//
// Connects to a local Temporal devserver, registers the GenerateCardsWorkflow
// and the activities it depends on, and listens on the prep-generation task
// queue. Run as a pm2 service (see ../ecosystem.config.js).
package main

import (
	"log"
	"os"
	"time"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	"prep-worker/activities"
	"prep-worker/agent"
	"prep-worker/shared"
	"prep-worker/workflows"
)

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lshortfile)
	log.Println("prep-worker booting")

	// Agent: HTTPAgent if PREP_AGENT_URL is set (containerized worker
	// talking to a host-side agent-server), else ShellAgent if a CLI
	// binary is configured (PREP_AGENT_BIN, CLAUDE_BIN, or the default
	// ~/.local/bin/claude path), else nil — the worker still boots and
	// non-AI activities still run, but AI activities will fail-fast
	// with a NoAgent error so the app can render a clear UI message.
	agentClient := agent.FromEnv()
	if agentClient == nil {
		// Last-resort default: probe the conventional claude-code path.
		// FromEnv only honors explicit env vars — this lets a contributor
		// who installed claude-code without setting PREP_AGENT_BIN still
		// get AI features automatically.
		if home, err := os.UserHomeDir(); err == nil {
			defaultBin := home + "/.local/bin/claude"
			if _, statErr := os.Stat(defaultBin); statErr == nil {
				agentClient = &agent.ShellAgent{Bin: defaultBin}
				log.Printf("agent: using default ShellAgent at %s", defaultBin)
			}
		}
	}
	if agentClient == nil {
		log.Println("agent: NONE configured — AI features disabled. Set PREP_AGENT_URL or PREP_AGENT_BIN to enable.")
	} else {
		log.Printf("agent: %T ready", agentClient)
	}

	cfg := &activities.Config{
		DBPath: os.Getenv("PREP_DB_PATH"),
		Agent:  agentClient,
	}
	if err := cfg.Validate(); err != nil {
		log.Fatalf("config invalid: %v", err)
	}

	hostPort := envOr("TEMPORAL_HOST_PORT", "127.0.0.1:7233")
	namespace := envOr("TEMPORAL_NAMESPACE", "prep")

	// Retry the dial — under docker compose / goreman, temporal may
	// start a few seconds after the worker. Bail fatally only after a
	// generous total wait so we surface real config issues without
	// crash-looping on a transient race.
	c, err := dialTemporalWithRetry(hostPort, namespace)
	if err != nil {
		log.Fatalf("dial temporal at %s: %v", hostPort, err)
	}
	defer c.Close()

	w := worker.New(c, shared.TaskQueue, worker.Options{})

	// Explicit workflow names so the FastAPI starter doesn't have to know
	// the Go package paths.
	w.RegisterWorkflowWithOptions(workflows.GradeAnswer, workflow.RegisterOptions{
		Name: shared.WorkflowGrade,
	})
	w.RegisterWorkflowWithOptions(workflows.Transform, workflow.RegisterOptions{
		Name: shared.WorkflowTransform,
	})
	w.RegisterWorkflowWithOptions(workflows.PlanGenerate, workflow.RegisterOptions{
		Name: shared.WorkflowPlanGenerate,
	})
	w.RegisterWorkflowWithOptions(workflows.TriviaGenerate, workflow.RegisterOptions{
		Name: shared.WorkflowTriviaGenerate,
	})

	a := &activities.Activities{Cfg: cfg}
	w.RegisterActivity(a)

	log.Printf("worker registered — namespace=%s task_queue=%s", namespace, shared.TaskQueue)

	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("worker exited: %v", err)
	}
}

// dialTemporalWithRetry handles the boot-time race where the worker
// comes up before temporal under goreman / docker compose. Total budget
// is ~60s, which comfortably covers the ~5-10s temporal devserver
// takes to be ready. Bails fatal after that — a real config issue
// (wrong host, wrong namespace) shouldn't sit in a hot retry forever.
func dialTemporalWithRetry(hostPort, namespace string) (client.Client, error) {
	var lastErr error
	deadline := time.Now().Add(60 * time.Second)
	delay := 500 * time.Millisecond
	for time.Now().Before(deadline) {
		c, err := client.Dial(client.Options{
			HostPort:  hostPort,
			Namespace: namespace,
		})
		if err == nil {
			return c, nil
		}
		lastErr = err
		log.Printf("temporal not ready yet (%v); retrying in %v", err, delay)
		time.Sleep(delay)
		if delay < 5*time.Second {
			delay *= 2
		}
	}
	return nil, lastErr
}
