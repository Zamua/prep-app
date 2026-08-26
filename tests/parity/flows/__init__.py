"""One module per flow; importing the package registers them all."""

from tests.parity.flows import (  # noqa: F401
    badge,
    dashboard,
    dashboard_empty,
    deck,
    deck_new,
    errors,
    grading,
    landing,
    offline,
    plan,
    privacy,
    question,
    reauth,
    reorganize,
    settings,
    sign_out,
    study,
    transform,
    trivia,
    trivia_generating,
)
