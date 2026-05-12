"""Workflow tracking bounded context.

Cross-cutting registry of in-flight Temporal workflows (transform,
plan, trivia generation, grading) so the masthead badge can show
the user what's running + push notifications can fire on
awaiting-action and terminal transitions.

The decks/study/trivia contexts call into `workflows.service` at
workflow start and at every fragment-status poll. The service writes
to a single `active_workflows` table; the badge route reads from it.
A periodic reconciler (`scheduler.py`) keeps the table accurate when
the user isn't actively polling.

DDD layout mirrors the other contexts:
- entities.py  → ActiveWorkflow, status enums, terminal sets
- repo.py      → ActiveWorkflowsRepo (CRUD + cleanup)
- service.py   → register + update_status + reconcile_active_workflows
- scheduler.py → periodic background reconciler loop
- routes.py    → GET /api/active-workflows-badge
"""

from prep.workflows.scheduler import start_workflows_scheduler

__all__ = ["start_workflows_scheduler"]
