// prep-worker — Temporal worker for the prep-app.
//
// Connects to a local Temporal devserver, registers the GenerateCardsWorkflow
// and the activities it depends on, and listens on the prep-generation task
// queue. Run as a pm2 service (see ../ecosystem.config.js).
package main

import (
	"log"
	"os"

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

	c, err := client.Dial(client.Options{
		HostPort:  hostPort,
		Namespace: namespace,
	})
	if err != nil {
		log.Fatalf("dial temporal at %s: %v", hostPort, err)
	}
	defer c.Close()

	w := worker.New(c, shared.TaskQueue, worker.Options{})

	// Explicit workflow names so the FastAPI starter doesn't have to know
	// the Go package paths.
	w.RegisterWorkflowWithOptions(workflows.GenerateCards, workflow.RegisterOptions{
		Name: shared.WorkflowGenerate,
	})
	w.RegisterWorkflowWithOptions(workflows.GradeAnswer, workflow.RegisterOptions{
		Name: shared.WorkflowGrade,
	})
	w.RegisterWorkflowWithOptions(workflows.Transform, workflow.RegisterOptions{
		Name: shared.WorkflowTransform,
	})

	a := &activities.Activities{Cfg: cfg}
	w.RegisterActivity(a)

	log.Printf("worker registered — namespace=%s task_queue=%s", namespace, shared.TaskQueue)

	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("worker exited: %v", err)
	}
}
