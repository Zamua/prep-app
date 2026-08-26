// /llms.txt: the agent manifest naming the MCP and REST surfaces, served
// unauthenticated so a client can read it before holding a token. The base
// URL is the product's own, as in prep/web/legal.py.
const BODY = `# prep

> Self-hosted spaced-repetition flashcards. Describe a topic, an AI
> turns it into a deck of cards; review on a forgetting curve.

prep exposes two programmatic surfaces for AI agents and scripts:

- **MCP server** at \`https://prepcards.app/mcp\` (streamable HTTP, JSON-RPC 2.0)
- **REST API** at \`https://prepcards.app/api/v1/*\` (Bearer-token auth)

Both share the same authentication: a personal access token issued
at \`https://prepcards.app/settings/api\`, used as \`Authorization: Bearer prep_pat_…\`.

## Setup (MCP)

In Claude Desktop, Claude Code, or any MCP client, add prep as a
remote MCP server:

\`\`\`jsonc
{
  "mcpServers": {
    "prep": {
      "url": "https://prepcards.app/mcp",
      "headers": {
        "Authorization": "Bearer prep_pat_REPLACE_ME"
      }
    }
  }
}
\`\`\`

## Tools exposed via MCP

- \`prep_list_decks\` — list the caller's decks
- \`prep_get_deck\` — deck metadata
- \`prep_list_cards\` — every card in a deck
- \`prep_export_deck_csv\` — deck contents as CSV text
- \`prep_create_deck\` — create an empty deck
- \`prep_import_csv\` — append CSV rows to a deck

## REST endpoints (v1)

- \`GET  https://prepcards.app/api/v1/decks\`
- \`POST https://prepcards.app/api/v1/decks\`
- \`GET  https://prepcards.app/api/v1/decks/<name>\`
- \`GET  https://prepcards.app/api/v1/decks/<name>/cards\`
- \`GET  https://prepcards.app/api/v1/decks/<name>/export.csv\`
- \`POST https://prepcards.app/api/v1/decks/<name>/import-csv\` (Content-Type: text/csv)

## Notes for agents

- Deck names are lowercase letters/digits/hyphens, 2–30 chars.
- The CSV wire format mirrors the JSON card shape: \`type, topic,
  prompt, answer, choices (newline-joined), rubric, skeleton,
  language, answer_regex, explanation\`.
- Tokens are long-lived until revoked. Revoke at \`https://prepcards.app/settings/api\`.
- Source: https://github.com/Zamua/prep-app
`;

/** `text/markdown` so a curl or an agent fetch needs no content negotiation. */
export function llmsTxt(): Response {
  return new Response(BODY, {
    headers: { 'content-type': 'text/markdown; charset=utf-8', 'cache-control': 'public, max-age=300' },
  });
}
