"""Static legal/info pages — /privacy.

Lives at prep/web/ rather than under a bounded context because the
content cross-cuts everything (auth, BYOK, the AI flows) and there's
no domain entity behind it. Stays auth-free so the landing footer can
link directly and unauthenticated visitors can read before signing up.

Only renders on Clerk-mode deploys today — the Tailscale-mode mac-mini
install is single-user and doesn't surface a public privacy notice.
That guard keeps the route from 404'ing surprises if a Tailscale user
follows a stale link, but its primary purpose is "this content is
written for the prepcards.app product specifically."
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from prep.web.templates import templates

router = APIRouter()


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy(request: Request):
    """Plain-English privacy page. No auth dependency — visitors who
    haven't signed up yet should be able to read it before pasting
    a key or creating an account."""
    if (os.environ.get("PREP_AUTH_MODE") or "").strip() != "clerk":
        # Self-hosted Tailscale install — there's nothing to disclose
        # we don't already control end-to-end.
        raise HTTPException(404)
    return templates.TemplateResponse("privacy.html", {"request": request, "user": None})


@router.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
def llms_txt(request: Request):
    """The /llms.txt convention (Jeremy Howard, late 2024): a markdown
    manifest at the site root that tells AI agents what this site is
    and how to use it programmatically. Different from robots.txt —
    it's a 'come in, here's the door' for agents, not a permission
    file.

    Public, no auth. Served as text/plain markdown so a curl or
    fetch from a coding-assistant works without content-negotiation
    surprises. Renders the MCP endpoint + the REST API base + how to
    issue a token."""
    base = "https://prepcards.app"
    body = f"""# prep

> Self-hosted spaced-repetition flashcards. Describe a topic, an AI
> turns it into a deck of cards; review on a forgetting curve.

prep exposes two programmatic surfaces for AI agents and scripts:

- **MCP server** at `{base}/mcp` (streamable HTTP, JSON-RPC 2.0)
- **REST API** at `{base}/api/v1/*` (Bearer-token auth)

Both share the same authentication: a personal access token issued
at `{base}/settings/api`, used as `Authorization: Bearer prep_pat_…`.

## Setup (MCP)

In Claude Desktop, Claude Code, or any MCP client, add prep as a
remote MCP server:

```jsonc
{{
  "mcpServers": {{
    "prep": {{
      "url": "{base}/mcp",
      "headers": {{
        "Authorization": "Bearer prep_pat_REPLACE_ME"
      }}
    }}
  }}
}}
```

## Tools exposed via MCP

- `prep_list_decks` — list the caller's decks
- `prep_get_deck` — deck metadata
- `prep_list_cards` — every card in a deck
- `prep_export_deck_csv` — deck contents as CSV text
- `prep_create_deck` — create an empty deck
- `prep_import_csv` — append CSV rows to a deck

## REST endpoints (v1)

- `GET  {base}/api/v1/decks`
- `POST {base}/api/v1/decks`
- `GET  {base}/api/v1/decks/<name>`
- `GET  {base}/api/v1/decks/<name>/cards`
- `GET  {base}/api/v1/decks/<name>/export.csv`
- `POST {base}/api/v1/decks/<name>/import-csv` (Content-Type: text/csv)

## Notes for agents

- Deck names are lowercase letters/digits/hyphens, 2–30 chars.
- The CSV wire format mirrors the JSON card shape: `type, topic,
  prompt, answer, choices (newline-joined), rubric, skeleton,
  language, answer_regex, explanation`.
- Tokens are long-lived until revoked. Revoke at `{base}/settings/api`.
- Source: https://github.com/Zamua/prep-app
"""
    return PlainTextResponse(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )
