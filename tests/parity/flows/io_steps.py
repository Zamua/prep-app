"""The shape the three importer flows share.

Each takes three shots: the empty form, the refusal the server renders when
the name is one it reserves, and the outcome the importer returns. The
refusal is reached with a reserved name rather than an empty file input
because the input carries `required`, so a browser never posts that case;
`static` passes the field's own pattern and is refused by the route.
"""

from __future__ import annotations

from tests.parity.harness.registry import FlowCtx
from tests.parity.harness.steps import open_page, shot, submit_form

RESERVED_NAME = "static"

SUBMIT = "form button[type=submit]"


def _fill(ctx: FlowCtx, *, deck_name: str, filename: str, mime: str, body: bytes) -> None:
    ctx.page.fill('form input[name="name"]', deck_name)
    ctx.page.set_input_files(
        'form input[type="file"]',
        files=[{"name": filename, "mimeType": mime, "buffer": body}],
    )


def import_flow(
    ctx: FlowCtx, *, path: str, deck_name: str, filename: str, mime: str, body: bytes
) -> None:
    badge = open_page(ctx, path, "main form")
    shot(ctx, "form", after_swap=badge)

    _fill(ctx, deck_name=RESERVED_NAME, filename=filename, mime=mime, body=body)
    submit_form(ctx, SUBMIT, "main .form-error")
    shot(ctx, "error")

    open_page(ctx, path, "main form")
    _fill(ctx, deck_name=deck_name, filename=filename, mime=mime, body=body)
    submit_form(ctx, SUBMIT, "main .agent-panel")
    shot(ctx, "outcome")
