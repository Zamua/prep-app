"""prep.agent.openrouter — OpenRouter BYOK adapter.

OpenRouter exposes an OpenAI-compatible chat-completions endpoint
that can route to any underlying provider via `model: "vendor/model"`
(e.g. `anthropic/claude-sonnet-4.5`, `openai/gpt-5`, `google/gemini-2.5-pro`).

We add OpenRouter's recommended attribution headers (HTTP-Referer +
X-Title) so prep traffic shows up under the prep app in the user's
OpenRouter dashboard — purely a courtesy, doesn't affect billing.
"""

from __future__ import annotations

from prep.agent.openai_compat import OpenAICompatAdapter


class OpenRouterAdapter(OpenAICompatAdapter):
    _api_base = "https://openrouter.ai/api/v1"
    _default_model = "anthropic/claude-sonnet-4.5"
    _prefix_check = ("sk-or-v1-",)
    _provider_label = "OpenRouter"

    def _extra_headers(self) -> dict[str, str]:
        return {
            "HTTP-Referer": "https://prepcards.app",
            "X-Title": "prep",
        }
