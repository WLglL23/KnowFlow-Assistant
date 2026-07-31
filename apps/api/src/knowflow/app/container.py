"""M01 dependency container."""

from dataclasses import dataclass

from knowflow.app.config import Settings


@dataclass(frozen=True, slots=True)
class AppContainer:
    """Composition-root state without eagerly connecting external services."""

    settings: Settings
