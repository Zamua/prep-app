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

	// Agent CLI: PREP_AGENT_BIN is the canonical name; CLAUDE_BIN is the
	// backward-compat alias for users carrying older configs forward.
	// Default to ~/.local/bin/claude — the path the Claude Code installer
	// uses on macOS/Linux. Mirrors the Python side's default in agent.py.
	agentBin := os.Getenv("PREP_AGENT_BIN")
	if agentBin == "" {
		agentBin = os.Getenv("CLAUDE_BIN")
	}
	if agentBin == "" {
		if home, err := os.UserHomeDir(); err == nil {
			agentBin = home + "/.local/bin/claude"
		}
	}
	cfg := &activities.Config{
		DBPath:    os.Getenv("PREP_DB_PATH"),
		AgentBin:  agentBin,
		AgentArgs: os.Getenv("PREP_AGENT_ARGS"),
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
