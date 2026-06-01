"""Log-redaction tests — pure unit, no fixtures needed."""

from __future__ import annotations

import io
import logging

from prep.web.log_redaction import SecretRedactingFormatter, install_on, redact

# ---- redact() pure helper ------------------------------------------------


def test_redact_replaces_api_key():
    raw = "submitting prompt with key sk-ant-api03-AbCdEf012345_678901234"
    out = redact(raw)
    assert "AbCdEf012345_678901234" not in out
    # Keeps the prefix so a human can tell which secret type it was.
    assert "sk-ant-api03-<REDACTED>" in out


def test_redact_replaces_oauth_token():
    raw = "auth failed for sk-ant-oat01-AbCdEf012345_678901234"
    out = redact(raw)
    assert "AbCdEf012345_678901234" not in out
    assert "sk-ant-oat01-<REDACTED>" in out


def test_redact_handles_multiple_in_one_line():
    raw = (
        "comparison: sk-ant-api03-AbCdEf012345_678901234 "
        "vs sk-ant-oat01-ZyXwVu987654_321098765432"
    )
    out = redact(raw)
    assert "AbCdEf" not in out
    assert "ZyXwVu" not in out
    assert out.count("<REDACTED>") == 2


def test_redact_is_idempotent():
    once = redact("key=sk-ant-api03-abcdefghijklmnopqrstuv")
    twice = redact(once)
    assert once == twice
    assert "<REDACTED>" in twice
    # Double-redaction shouldn't introduce a second placeholder.
    assert twice.count("<REDACTED>") == 1


def test_redact_skips_already_masked_form():
    """The mask helper renders keys as `sk-ant-api03-…x9zT` for display
    — the ellipsis sits where we'd otherwise see 20+ base64 chars,
    so the regex doesn't match it. Important: don't double-redact
    safe-to-display masked forms into illegible noise."""
    raw = "user key prefix: sk-ant-api03-…x9zT"
    out = redact(raw)
    assert out == raw


def test_redact_leaves_unrelated_strings_alone():
    assert redact("nothing to see here") == "nothing to see here"
    assert redact("") == ""
    # Anthropic dashboard URLs etc. — substring match would falsely
    # hit the prefix; only the 20+-char suffix shape triggers.
    assert redact("https://console.anthropic.com/settings/keys") == (
        "https://console.anthropic.com/settings/keys"
    )


# ---- integration with logging.Logger -------------------------------------


def test_install_on_scrubs_handler_output():
    """End-to-end: logger.info(...) with a secret in the message lands
    in the handler's stream with the secret replaced."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("prep.test.redaction.basic")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    install_on(logger)
    logger.info("token=sk-ant-api03-AbCdEf012345_678901234 in the wild")

    out = buf.getvalue()
    assert "AbCdEf012345_678901234" not in out
    assert "<REDACTED>" in out


def test_install_on_is_idempotent():
    """Wrapping the same handler twice should not produce a double-
    wrapped formatter — the install helper checks via isinstance."""
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("prep.test.redaction.idempotent")
    logger.handlers = [handler]

    install_on(logger)
    wrapped_once = handler.formatter
    install_on(logger)
    wrapped_twice = handler.formatter

    assert wrapped_once is wrapped_twice
    assert isinstance(wrapped_twice, SecretRedactingFormatter)


def test_install_on_handles_handler_without_formatter():
    """A handler attached without setFormatter() has formatter=None.
    install_on should fall back to a default Formatter rather than
    blowing up."""
    handler = logging.StreamHandler(io.StringIO())
    # Intentionally no setFormatter.
    logger = logging.getLogger("prep.test.redaction.noformatter")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)

    install_on(logger)
    assert isinstance(handler.formatter, SecretRedactingFormatter)
    # And it still scrubs secrets.
    logger.info("payload: sk-ant-oat01-AbCdEf012345_678901234")
    assert "AbCdEf012345_678901234" not in handler.stream.getvalue()


def test_redaction_survives_exception_traces():
    """When an exception's args carry a secret, the formatted traceback
    that lands on stdout shouldn't contain plaintext."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("prep.test.redaction.traceback")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    install_on(logger)

    try:
        raise ValueError("bad key: sk-ant-api03-AbCdEf012345_678901234")
    except ValueError:
        logger.exception("upstream rejected")

    out = buf.getvalue()
    assert "AbCdEf012345_678901234" not in out
    assert "<REDACTED>" in out
