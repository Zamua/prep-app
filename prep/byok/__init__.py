"""Bring-your-own-key (BYOK) bounded context.

Per-user AI provider credentials — Anthropic API keys today,
OpenRouter later. Each user supplies their own key; prep stores it
encrypted at rest and uses it only on that user's AI calls.

Subpackages:
- `crypto`   — AES-256-GCM envelope encryption with a process-level
               master key (PREP_KEY_ENCRYPTION_SECRET)
- `entities` — Credential value object + Provider enum
- `repo`     — BYOKRepo: store/get/delete (encrypt boundary)
- `service`  — application service: validation, masking, audit

Threat model + design rationale lives in the package modules.
"""
