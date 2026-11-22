"""An assembly as rigid groups joined by joints, in neutral terms, and the question "what moves if this joint turns?".

Positions are millimetres. A transform is 16 numbers, row by row, taking a part's own coordinates to the
assembly's; its translation is in millimetres.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .backends.base import ModelRef


@dataclass
class Instance:
    id: str
    name: str
    studio: ModelRef  # where the part's geometry lives, pinned to one version
    part: str  # the part's id inside that studio
    transform: list[float]
    fixed: bool = False


@dataclass
class Joint:
    name: str
    kind: str  # "revolute", "slider", "fastened", ...
    sides: tuple[str, str]  # the two instances it joins
    origin: tuple[float, float, float]  # a point on the axis, in the assembly
    axis: tuple[float, float, float]  # unit vector
    mismatch: float = 0.0  # how far apart the two sides' connectors are right now, in mm (0 when the joint is solved)


@dataclass
class Assembly:
    instances: dict[str, Instance]
    groups: list[tuple[str, list[str]]] = field(default_factory=list)  # (name, instance ids) held rigidly together
    joints: list[Joint] = field(default_factory=list)

    def find_joint(self, selector: str) -> Joint:
        moving = [j for j in self.joints if j.kind != "fastened"]
        text = selector.strip()
        if text.isdigit() and 1 <= int(text) <= len(moving):
            return moving[int(text) - 1]
        hits = [j for j in moving if j.name == text] or [j for j in moving if text.lower() in j.name.lower()]
        if len(hits) == 1:
            return hits[0]
        listing = "; ".join(f"{n} \"{j.name}\"" for n, j in enumerate(moving, start=1)) or "none"
        raise LookupError(f"{'No' if not hits else 'More than one'} joint matches \"{selector}\". Joints: {listing}")


def transform_point(t: list[float], p) -> tuple[float, float, float]:
    return tuple(t[4 * r] * p[0] + t[4 * r + 1] * p[1] + t[4 * r + 2] * p[2] + t[4 * r + 3] for r in range(3))


def transform_direction(t: list[float], d) -> tuple[float, float, float]:
    return tuple(t[4 * r] * d[0] + t[4 * r + 1] * d[1] + t[4 * r + 2] * d[2] for r in range(3))


def split(assembly: Assembly, joint: Joint) -> tuple[list[str], list[str], list[str]]:
    """(moving instance ids, still instance ids, notes) when only this joint turns and every other joint holds."""
    parent = {i: i for i in assembly.instances}

    def find(i: str) -> str:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for _, members in assembly.groups:
        for other in members[1:]:
            join(members[0], other)
    for other in assembly.joints:  # every joint except the one in question is taken as held where it is
        if other is not joint:
            join(*other.sides)
    grounded = {find(i) for i, instance in assembly.instances.items() if instance.fixed}
    if not grounded:
        raise ValueError("Nothing in this assembly is fixed, so there is no telling which side of the joint moves. "
                         "Fix one part (the base) in the CAD system.")
    a, b = find(joint.sides[0]), find(joint.sides[1])
    if a == b:
        raise ValueError(f"The two sides of \"{joint.name}\" are also held together some other way, so it cannot turn.")
    if a in grounded and b in grounded:
        raise ValueError(f"Both sides of \"{joint.name}\" are fixed.")
    mover = b if a in grounded else a if b in grounded else None
    notes = []
    if mover is None:
        mover = b
        notes.append(f"Neither side of \"{joint.name}\" is connected to the fixed part; the second side is taken as the moving one.")
    moving = [i for i in assembly.instances if find(i) == mover]
    still = [i for i in assembly.instances if find(i) != mover]
    loose = [assembly.instances[i].name for i in still if find(i) not in grounded and find(i) != (a if mover == b else b)]
    if loose:
        notes.append(f"{len(loose)} part(s) are not held by any joint and are treated as standing still: " + ", ".join(loose[:5])
                     + (" ..." if len(loose) > 5 else ""))
    return moving, still, notes
