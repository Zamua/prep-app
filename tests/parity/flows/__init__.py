"""One module per flow; importing the package registers them all."""

from tests.parity.flows import (  # noqa: F401
    dashboard,
    dashboard_empty,
    deck,
    deck_new,
    errors,
    landing,
    offline,
    privacy,
    question,
    reauth,
    settings,
    sign_out,
    study,
    trivia,
)
