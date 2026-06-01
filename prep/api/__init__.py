"""Public REST API + bearer-token auth.

This is the bounded context for prep's machine-facing surface —
distinct from `prep.web/` (the HTML UI) and `prep.agent/` (the AI
backend). Users generate personal access tokens at /settings/api
and use them on `/api/v1/*` calls. The same tokens drive the MCP
server (slice 3).

Modules:
- `entities`: ApiTokenMetadata value object
- `repo`: ApiTokenRepo — create / list / delete / lookup_by_token
- `auth`: bearer_user FastAPI dependency
- `routes`: /api/v1/* endpoints + /settings/api UI
"""
