"""Snapshot store: what we read from the CAD document, kept on disk by version.

Reading costs requests; re-reading the same version must cost none. Layout:

    <root>/<backend>/<document>/<element>/latest          the version read most recently
    <root>/<backend>/<document>/<element>/history         every version read, oldest first
    <root>/<backend>/<document>/<element>/<version>/...   features.json, mesh.stl, mesh_report.json
"""
from __future__ import annotations

import json
from pathlib import Path

from .backends.base import ModelRef, Snapshot


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _folder(self, ref: ModelRef) -> Path:
        return self.root / ref.backend / ref.document / ref.element

    def path(self, ref: ModelRef, version: str, name: str) -> Path:
        return self._folder(ref) / version / name

    def save(self, snapshot: Snapshot) -> None:
        target = self.path(snapshot.ref, snapshot.version, "features.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(snapshot.data))
        folder = self._folder(snapshot.ref)
        history = self.versions(snapshot.ref)
        if not history or history[-1] != snapshot.version:
            with open(folder / "history", "a") as fh:
                fh.write(snapshot.version + "\n")
        (folder / "latest").write_text(snapshot.version)

    def versions(self, ref: ModelRef) -> list[str]:
        history = self._folder(ref) / "history"
        return history.read_text().split() if history.exists() else []

    def latest(self, ref: ModelRef) -> Snapshot | None:
        marker = self._folder(ref) / "latest"
        if not marker.exists():
            return None
        version = marker.read_text().strip()
        return Snapshot(ref, version, json.loads(self.path(ref, version, "features.json").read_text()))

    def previous(self, ref: ModelRef, version: str) -> str | None:
        """The version read just before this one (history never holds the same version twice in a row)."""
        history = self.versions(ref)
        if version not in history:
            return None
        last = len(history) - 1 - history[::-1].index(version)
        return history[last - 1] if last > 0 else None

    def mark_stale(self, ref: ModelRef) -> None:
        """Forget which version is current. The versions stay on disk; the next read must be live."""
        (self._folder(ref) / "latest").unlink(missing_ok=True)
