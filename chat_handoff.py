"""Build "discuss this card in your AI chat" handoff URLs.

Renders one prefilled-message URL per supported provider; the result page
embeds all of them as data attributes and the client picks based on the
user's localStorage preference. Default Claude.

URL templates rely on each provider's web-side prefill conventions:
- Claude: claude.ai/new accepts `?q=` for an initial prompt
- ChatGPT: chatgpt.com accepts `?q=`
- Perplexity: perplexity.ai accepts `?q=`
On iOS / Android, opening a claude.ai or chatgpt.com link triggers a
universal-link handoff into the installed app if present.
"""

from __future__ import annotations

import urllib.parse


CHAT_PROVIDERS: dict[str, dict[str, str]] = {
    "claude":     {"label": "Claude",     "url": "https://claude.ai/new?q={q}"},
    "chatgpt":    {"label": "ChatGPT",    "url": "https://chatgpt.com/?q={q}"},
    "perplexity": {"label": "Perplexity", "url": "https://www.perplexity.ai/?q={q}"},
}

DEFAULT_PROVIDER = "claude"

# Long code answers can blow past mobile-browser URL caps (~80KB on iOS Safari,
# tighter on Android). Truncate aggressively per-section to keep the total
# message well under that. The user can paste the rest into chat manually.
_MAX_FIELD_CHARS = 4000


def _trim(s: str | None) -> str:
    if not s:
        return ""
    if len(s) <= _MAX_FIELD_CHARS:
        return s
    return s[:_MAX_FIELD_CHARS].rstrip() + "\n…[truncated]"


def build_message(
    *,
    deck_name: str,
    q: dict,
    user_answer: str = "",
    verdict: dict | None = None,
    idk: bool = False,
    picked_set: list[str] | None = None,
    correct_set: list[str] | None = None,
) -> str:
    """Compose the markdown message that gets prefilled into the AI chat."""
    qtype = q.get("type", "short")
    parts: list[str] = []
    parts.append("I'm reviewing an interview-prep flashcard and want to talk through it.\n")

    parts.append(f"**Question** (deck: `{deck_name}`, type: `{qtype}`):")
    parts.append(_trim(q.get("prompt", "")))

    if qtype in ("mcq", "multi") and q.get("choices_list"):
        parts.append("\n**Choices:**")
        for c in q["choices_list"]:
            mark = ""
            if picked_set and c in picked_set:
                mark = " ← my pick"
            if correct_set and c in correct_set:
                mark += " ✓ correct"
            parts.append(f"- {c}{mark}")

    if idk:
        parts.append("\n**My answer:** _(I don't know — skipped)_")
    elif user_answer and qtype not in ("mcq", "multi"):
        # For mcq/multi the picked_set is already shown inline above.
        parts.append("\n**My answer:**")
        if qtype == "code":
            parts.append("```")
            parts.append(_trim(user_answer))
            parts.append("```")
        else:
            parts.append(_trim(user_answer))

    if q.get("answer") and qtype not in ("mcq", "multi"):
        # mcq/multi correct answers are already shown inline with choices.
        parts.append("\n**Model answer:**")
        if qtype == "code":
            parts.append("```")
            parts.append(_trim(q["answer"]))
            parts.append("```")
        else:
            parts.append(_trim(q["answer"]))

    if verdict:
        result = verdict.get("result", "unknown")
        parts.append(f"\n**Verdict:** {result}")
        if verdict.get("feedback"):
            parts.append(f"**Feedback:** {_trim(verdict['feedback'])}")

    if q.get("rubric"):
        parts.append(f"\n**Rubric:** {_trim(q['rubric'])}")

    parts.append("\nWhat am I missing? How would you approach this?")
    return "\n".join(parts)


def provider_urls(message: str) -> dict[str, str]:
    """Return {provider_key: prefilled_url} for every known provider."""
    encoded = urllib.parse.quote(message, safe="")
    return {
        key: cfg["url"].format(q=encoded)
        for key, cfg in CHAT_PROVIDERS.items()
    }
