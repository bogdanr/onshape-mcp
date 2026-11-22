"""CAD backends. Add one by implementing ``base.CadBackend`` and naming it here."""
from __future__ import annotations

from ..budget import Ledger
from ..config import ConfigError
from .base import CadBackend

AVAILABLE = ("onshape",)


def create(settings: dict, ledger: Ledger) -> CadBackend:
    kind = settings.get("type")
    if kind == "onshape":
        from .onshape.backend import OnshapeBackend

        return OnshapeBackend.from_settings(settings, ledger)
    raise ConfigError(f"Unknown [backend] type '{kind}'. Available: {', '.join(AVAILABLE)}")
