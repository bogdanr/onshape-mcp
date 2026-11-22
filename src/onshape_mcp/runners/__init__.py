"""Runners. Add one by implementing ``base.Runner`` and naming it here."""
from __future__ import annotations

from ..config import ConfigError
from .base import Runner

AVAILABLE = ("local", "ssh")


def create(settings: dict) -> Runner:
    kind = settings.get("type")
    options = {k: v for k, v in settings.items() if k != "type"}
    try:
        if kind == "local":
            from .local import LocalRunner

            return LocalRunner(**options)
        if kind == "ssh":
            from .ssh import SshRunner

            return SshRunner(**options)
    except TypeError as e:
        raise ConfigError(f"[runner] has a setting the '{kind}' runner does not know: {e}") from None
    raise ConfigError(f"Unknown [runner] type '{kind}'. Available: {', '.join(AVAILABLE)}")
