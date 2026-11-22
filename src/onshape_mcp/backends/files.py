"""Mesh files on disk as a CAD backend.

The smallest possible backend, and a useful one: any STL, from a script, a slicer or another CAD program, can be
checked and pictured with the same tools. There is no feature tree and nothing can be written. The "version" is
the file's content hash, so a file that has not changed is never measured twice.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .base import CadBackend, FeatureOutline, ModelRef, Snapshot


class FileBackend(CadBackend):
    name = "file"

    def accepts(self, text: str) -> bool:
        return text.lower().endswith(".stl") and "://" not in text

    def parse_ref(self, text: str) -> ModelRef:
        path = Path(text).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"There is no file {path}")
        return ModelRef("file", hashlib.sha1(str(path.parent).encode()).hexdigest()[:16], str(path), path.name)

    def read_studio(self, ref: ModelRef) -> Snapshot:
        digest = hashlib.sha1(Path(ref.workspace).read_bytes()).hexdigest()[:24]
        return Snapshot(ref, digest, {"features": [], "file": ref.workspace})

    def outline(self, snapshot: Snapshot) -> list[FeatureOutline]:
        return []

    def describe_feature(self, snapshot: Snapshot, selector: str) -> str:
        return "A mesh file has no features."

    def export_mesh(self, ref: ModelRef, version: str) -> bytes:
        return Path(ref.workspace).read_bytes()
