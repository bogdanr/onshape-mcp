"""The CAD plug. A backend is one CAD system; tools never talk to a CAD system any other way.

Only what the current tools need is here. New abilities (writing, assemblies, exact measures) are added to this
interface when a tool needs them, so every backend states plainly what it can do.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..expr import Quantity
from ..sketch import Sketch


@dataclass(frozen=True)
class ModelRef:
    """One editable unit in a CAD system, for example one Part Studio tab of an Onshape document."""

    backend: str
    document: str
    workspace: str  # the branch or working copy; for Onshape "w/<id>", "v/<id>" or "m/<id>"
    element: str


@dataclass
class Snapshot:
    """The feature tree exactly as the backend returned it, at one version of the document."""

    ref: ModelRef
    version: str
    data: dict


@dataclass
class FeatureOutline:
    """One line of the feature tree, in backend-neutral words."""

    index: int  # 1-based position in the tree
    id: str
    kind: str  # "sketch", "extrude", "variable", ...
    name: str
    status: str  # "OK", "WARNING", "ERROR", ...
    suppressed: bool = False
    numbers: list[tuple[str, str]] = field(default_factory=list)  # (label, value): what a person would edit
    notes: list[str] = field(default_factory=list)


@dataclass
class ChangePlan:
    """Number changes that passed every local check, ready to send in one request."""

    lines: list[str]  # one readable line per change: where, old value, new value
    payload: object  # whatever the backend needs to send them
    notes: list[str] = field(default_factory=list)  # things the reader should know before trusting the result


@dataclass
class WriteResult:
    """What the CAD system said after a write."""

    statuses: dict[str, str]  # feature id -> "OK", "WARNING", "ERROR", ...
    feature_id: str | None = None  # the feature just added, if one was
    version: str | None = None  # the document's version after the write, to pin the next write to

    def failed(self) -> list[str]:
        return [fid for fid, status in self.statuses.items() if status not in ("OK", "INFO")]


class Unsupported(RuntimeError):
    """This CAD backend does not have that ability."""


class CadBackend(ABC):
    name: str

    @abstractmethod
    def parse_ref(self, text: str) -> ModelRef:
        """Turn what a person pastes (a URL, a path) into a ModelRef. No requests."""

    @abstractmethod
    def read_studio(self, ref: ModelRef) -> Snapshot:
        """Read the whole feature tree in as few requests as the CAD system allows."""

    @abstractmethod
    def outline(self, snapshot: Snapshot) -> list[FeatureOutline]:
        """Summarise a snapshot. No requests."""

    @abstractmethod
    def describe_feature(self, snapshot: Snapshot, selector: str) -> str:
        """Everything about one feature (by position, id or name), as readable text. No requests."""

    @abstractmethod
    def export_mesh(self, ref: ModelRef, version: str) -> bytes:
        """The solid bodies at exactly this version, as one binary STL in millimetres."""

    def problem(self) -> str | None:
        """Why this backend cannot reach its CAD system right now (no keys, say), or None. No requests."""
        return None

    # ---- No requests, with a plain default for backends that have no feature tree.

    def variables(self, snapshot: Snapshot) -> dict[str, Quantity]:
        """The values of the variables defined in this tab, for checking expressions."""
        return {}

    def feature_names(self, snapshot: Snapshot) -> dict[str, str]:
        """Feature name -> feature id."""
        return {}

    # ---- Writing. Each "add" or "apply" is one request; nothing here retries. A backend that cannot write
    # leaves these as they are, and the tools say so.

    def _cannot(self, what: str):
        raise Unsupported(f"The {self.name} backend cannot {what}.")

    def sketch_plane(self, snapshot: Snapshot, selector: str) -> tuple[str, str, float]:
        """(feature id, base plane, offset in mm) of an existing sketch. No requests."""
        self._cannot("look up sketches")

    def plan_changes(self, snapshot: Snapshot, changes: list[dict]) -> ChangePlan:
        """Resolve and check number changes ({feature, number, to, was?}). No requests."""
        self._cannot("change numbers")

    def apply_changes(self, snapshot: Snapshot, plan: ChangePlan) -> WriteResult:
        """Send the changes in one request. Must fail, not overwrite, if the document changed since the snapshot."""
        self._cannot("change numbers")

    def plane_info(self, snapshot: Snapshot, selector: str) -> tuple[str, str, float]:
        """(feature id, base plane, signed offset in mm) of an existing offset plane. No requests."""
        self._cannot("look up planes")

    def add(self, ref: ModelRef, what: str, after: str | None = None, **spec) -> WriteResult:
        """Add one feature at the end of the tree: ``what`` is variable, plane, sketch, extrude, fillet or chamfer.
        ``after`` is the version last seen; the write must be refused if the document has moved on from it."""
        self._cannot("add features")

    def name_parts(self, ref: ModelRef, names: dict[str, str]) -> dict[str, str]:
        """Name the parts made by these features ({feature id: part name}). Returns {feature id: what happened}."""
        self._cannot("name parts")

    def plan_repair(self, snapshot: Snapshot) -> ChangePlan:
        """Find what makes the model hard to edit by hand and can be fixed without changing its shape. No requests."""
        self._cannot("repair models")

    def apply_repair(self, snapshot: Snapshot, plan: ChangePlan) -> WriteResult:
        self._cannot("repair models")

    def explain(self, ref: ModelRef, feature_ids: list[str]) -> dict[str, str]:
        """The CAD system's own words for why these features failed. One request."""
        self._cannot("explain failures")

    # ---- Assemblies.

    def read_assembly(self, ref: ModelRef) -> Snapshot:
        """The assembly's parts, positions and joints, in one request."""
        self._cannot("read assemblies")

    def assembly(self, snapshot: Snapshot):
        """The snapshot as a ``motion.Assembly``. No requests."""
        self._cannot("read assemblies")

    def part_boxes(self, studio: ModelRef) -> list[dict]:
        """Every solid part of a studio: [{"id", "name", "lo", "hi", "volume"}] in mm. One request."""
        self._cannot("list parts")
