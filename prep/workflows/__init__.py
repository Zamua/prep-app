"""Workflow tracking bounded context.

Cross-cutting registry of in-flight Temporal workflows (transform,
plan, trivia generation, grading) so the masthead badge can show
the user what's running + push notifications can fire on
awaiting-action and terminal transitions.

The decks/study/trivia contexts call into `workflows.service` at
workflow start and at every fragment-status poll. The service writes
to a single `active_workflows` table; the badge route reads from it.

DDD layout mirrors the other contexts:
- entities.py  → ActiveWorkflow, status enums, terminal sets
- repo.py      → ActiveWorkflowsRepo (CRUD + cleanup)
- service.py   → register + update_status (handles push fan-out)
- routes.py    → GET /api/active-workflows-badge
"""
