"""prep.agent.openai_api — OpenAI Chat Completions BYOK adapter."""

from __future__ import annotations

from prep.agent.openai_compat import OpenAICompatAdapter


class OpenAIAdapter(OpenAICompatAdapter):
    _api_base = "https://api.openai.com/v1"
    _default_model = "gpt-5-mini"
    _prefix_check = ("sk-",)
    _provider_label = "OpenAI"
