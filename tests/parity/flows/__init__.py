"""One module per flow; importing the package registers them all."""

from tests.parity.flows import (  # noqa: F401
    dashboard,
    deck,
    errors,
    landing,
    offline,
    privacy,
    study,
)
