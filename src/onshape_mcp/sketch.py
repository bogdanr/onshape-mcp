"""A sketch, and the gate every sketch passes before it is sent to a CAD system.

The gate answers three questions with no CAD system involved:

1. Do the shapes agree with the constraints?  If the drawing is slightly off (56.11 where the dimensions say
   56.1127) it is solved here first, so the CAD system receives exact geometry and has nothing left to move.
2. Can anything still move?  A sketch must be fully defined: 0 free movements.
3. Does any constraint only repeat what the others already fix?  0 of those.

Lengths are millimetres. The method is the one a CAD solver uses: the constraints are equations in the shapes'
coordinates, and the rank of their Jacobian says how many movements they remove.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .expr import Quantity, length

PLANES = ("Top", "Front", "Right")
PARAMS = {"point": ("x", "y"), "line": ("start x", "start y", "end x", "end y"),
          "circle": ("centre x", "centre y", "radius"),
          "arc": ("centre x", "centre y", "radius", "start angle", "end angle")}
SUBPOINTS = {"point": (), "line": ("start", "end"), "circle": ("center",), "arc": ("center", "start", "end")}
# constraint -> (number of references, takes a value)
CONSTRAINTS = {
    "coincident": (2, False), "horizontal": ((1, 2), False), "vertical": ((1, 2), False),
    "parallel": (2, False), "perpendicular": (2, False), "equal": (2, False), "midpoint": (3, False),
    "diameter": (1, True), "radius": (1, True), "length": (1, True),
    "distance": (2, True), "distance_x": (2, True), "distance_y": (2, True),
}
ORIGIN = "origin"
ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
MAX_START_ERROR = 1.0  # mm: a drawing further than this from its own dimensions is refused, not "repaired"
SOLVED = 1e-7


class SketchError(ValueError):
    """The sketch description itself is wrong (unknown name, bad shape). Nothing was checked or sent."""


@dataclass
class Entity:
    id: str
    kind: str
    params: list[float]
    construction: bool = False


@dataclass
class Constraint:
    kind: str
    refs: list[str]
    expression: str | None = None  # as the owner will see it in the dimension, e.g. "#thickness + 2 mm"
    value: float | None = None  # the same in millimetres

    def text(self) -> str:
        return f"{self.kind}({', '.join(self.refs)})" + (f" = {self.expression}" if self.expression else "")


@dataclass
class Sketch:
    name: str
    plane: str
    entities: dict[str, Entity]
    constraints: list[Constraint]

    def uses_origin(self) -> bool:
        return any(ORIGIN in c.refs for c in self.constraints)


@dataclass
class Gate:
    """What the gate found. ``passed`` is the only thing that lets a sketch through."""

    free: int = 0
    moving: dict[str, list[str]] = field(default_factory=dict)  # entity -> the coordinates that can still move
    repeated: list[str] = field(default_factory=list)  # constraints that add nothing
    off: list[tuple[str, float]] = field(default_factory=list)  # constraint, how far the drawing is from it
    moved: float = 0.0  # the largest correction made to the drawing, in mm
    unsolved: bool = False

    @property
    def passed(self) -> bool:
        return not (self.free or self.repeated or self.off or self.unsolved)

    def lines(self) -> list[str]:
        if self.passed:
            note = f" (the drawing was corrected by up to {self.moved:.3f} mm to match its dimensions)" if self.moved > 1e-6 else ""
            return ["Fully defined: 0 free movements, 0 repeated constraints" + note + "."]
        out = []
        if self.off:
            out.append("The drawing does not match its own constraints (give coordinates that agree with the dimensions):")
            out += [f"  {text} is off by {error:.3f}" for text, error in self.off]
        if self.unsolved:
            out.append("The constraints cannot all hold at once near this drawing. Look for two that contradict each other.")
        if self.free:
            out.append(f"NOT fully defined: {self.free} movement{'s' if self.free > 1 else ''} still free. These can still move:")
            out += [f"  {entity}: {', '.join(names)}" for entity, names in self.moving.items()]
            out.append("  Add a dimension or constraint for each, from the origin or from another shape.")
        if self.repeated:
            out.append("These constraints only repeat what the ones before them already fix. Remove them:")
            out += [f"  {text}" for text in self.repeated]
        return out


# ---------------------------------------------------------------- reading a description

def from_spec(spec: dict, variables: dict[str, Quantity] | None = None, planes=PLANES) -> Sketch:
    """Build a Sketch from the plain description a tool receives. See the cad_build tool for the format."""
    name, plane = spec.get("name"), spec.get("plane", "Top")
    if not name or not isinstance(name, str):
        raise SketchError("A sketch needs a name that says what it is.")
    if plane not in planes:
        raise SketchError(f"Sketch '{name}': plane must be one of {', '.join(planes)}.")
    entities: dict[str, Entity] = {}
    constraints: list[Constraint] = []

    def add(eid: str, kind: str, params: list, construction: bool) -> None:
        if eid in entities or eid == ORIGIN:
            raise SketchError(f"Sketch '{name}': two shapes are called '{eid}'.")
        if len(params) != len(PARAMS[kind]) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in params):
            raise SketchError(f"Sketch '{name}': {kind} '{eid}' needs {len(PARAMS[kind])} numbers ({', '.join(PARAMS[kind])}).")
        values = [float(v) for v in params]
        if kind == "line" and math.hypot(values[2] - values[0], values[3] - values[1]) < 1e-6:
            raise SketchError(f"Sketch '{name}': line '{eid}' has no length.")
        if kind in ("circle", "arc") and values[2] <= 0:
            raise SketchError(f"Sketch '{name}': {kind} '{eid}' needs a radius greater than 0.")
        if kind == "arc":
            values[3:5] = math.radians(values[3]), math.radians(values[4])
            while values[4] <= values[3]:  # arcs run counter-clockwise from the first angle to the second
                values[4] += 2 * math.pi
        entities[eid] = Entity(eid, kind, values, construction)

    for item in spec.get("entities", []):
        eid, construction = item.get("id"), bool(item.get("construction", False))
        kinds = [k for k in ("point", "line", "circle", "arc", "rectangle") if k in item]
        if not isinstance(eid, str) or not ID.match(eid) or len(kinds) != 1:
            raise SketchError(f"Sketch '{name}': each shape needs an id (letters, digits, _) and exactly one of "
                              f"point, line, circle, arc, rectangle: {item}")
        values = item[kinds[0]]
        if kinds[0] == "rectangle":
            if len(values) != 4 or values[0] >= values[2] or values[1] >= values[3]:
                raise SketchError(f"Sketch '{name}': rectangle '{eid}' needs x0, y0, x1, y1 with x0 < x1 and y0 < y1.")
            x0, y0, x1, y1 = values
            sides = {"bottom": [x0, y0, x1, y0], "right": [x1, y0, x1, y1], "top": [x1, y1, x0, y1], "left": [x0, y1, x0, y0]}
            for side, line in sides.items():
                add(f"{eid}.{side}", "line", line, construction)
            order = list(sides)
            for i, side in enumerate(order):  # a closed loop of two horizontal and two vertical lines
                constraints.append(Constraint("coincident", [f"{eid}.{side}.end", f"{eid}.{order[(i + 1) % 4]}.start"]))
                constraints.append(Constraint("horizontal" if side in ("bottom", "top") else "vertical", [f"{eid}.{side}"]))
        else:
            add(eid, kinds[0], values, construction)

    sketch = Sketch(name, plane, entities, constraints)
    for item in spec.get("constraints", []):
        if not isinstance(item, (list, tuple)) or not item or item[0] not in CONSTRAINTS:
            raise SketchError(f"Sketch '{name}': unknown constraint {item}. Known: {', '.join(CONSTRAINTS)}.")
        kind, (count, has_value) = item[0], CONSTRAINTS[item[0]]
        refs = [str(r) for r in (item[1:-1] if has_value else item[1:])]
        counts = count if isinstance(count, tuple) else (count,)
        if len(refs) not in counts or (has_value and len(item) < 3):
            raise SketchError(f"Sketch '{name}': {kind} takes {' or '.join(map(str, counts))} shape(s)"
                              + (" and a value" if has_value else "") + f": {item}")
        constraint = Constraint(kind, refs)
        if has_value:
            constraint.expression = str(item[-1])
            constraint.value = length(constraint.expression, variables)
            if constraint.value <= 0:
                raise SketchError(f"Sketch '{name}': {constraint.text()} must be greater than 0. "
                                  "To line two points up, use horizontal or vertical instead of a distance of 0.")
        _validate(sketch, constraint)
        constraints.append(constraint)
    if not entities:
        raise SketchError(f"Sketch '{name}' has no shapes.")
    return sketch


def split(sketch: Sketch, ref: str) -> tuple[Entity | None, str | None]:
    """'plate.bottom.start' -> (the line plate.bottom, 'start'); 'origin' -> (None, None)."""
    if ref == ORIGIN:
        return None, None
    if ref in sketch.entities:
        return sketch.entities[ref], None
    owner, _, sub = ref.rpartition(".")
    if owner in sketch.entities and sub in SUBPOINTS[sketch.entities[owner].kind]:
        return sketch.entities[owner], sub
    raise SketchError(f"Sketch '{sketch.name}': there is no '{ref}'. Shapes: {', '.join(sketch.entities)}. "
                      "Points on a shape: line.start, line.end, circle.center, arc.center, arc.start, arc.end, and origin.")


def is_point(sketch: Sketch, ref: str) -> bool:
    entity, sub = split(sketch, ref)
    return entity is None or sub is not None or entity.kind == "point"


def _validate(sketch: Sketch, c: Constraint) -> None:
    kinds = []
    for ref in c.refs:
        entity, _ = split(sketch, ref)
        kinds.append("point" if is_point(sketch, ref) else entity.kind)
    wrong = None
    if c.kind == "coincident" and kinds.count("point") == 0:
        wrong = "needs at least one point"
    elif c.kind in ("horizontal", "vertical") and kinds not in (["line"], ["point", "point"]):
        wrong = "takes one line, or two points"
    elif c.kind in ("parallel", "perpendicular") and kinds != ["line", "line"]:
        wrong = "takes two lines"
    elif c.kind == "equal" and (kinds[0] != kinds[1] and set(kinds) != {"circle", "arc"} or "point" in kinds):
        wrong = "takes two lines, or two circles or arcs"
    elif c.kind in ("midpoint", "distance", "distance_x", "distance_y") and set(kinds) != {"point"}:
        wrong = "takes points (for example line.start, circle.center, origin)"
    elif c.kind == "diameter" and kinds != ["circle"]:
        wrong = "is for a full circle (use radius for an arc)"
    elif c.kind == "radius" and kinds != ["arc"]:
        wrong = "is for an arc (use diameter for a full circle)"
    elif c.kind == "length" and kinds != ["line"]:
        wrong = "is for a line"
    if wrong:
        raise SketchError(f"Sketch '{sketch.name}': {c.text()} {wrong}.")


# ---------------------------------------------------------------- the equations

def _point(sketch: Sketch, ref: str, p: dict[str, list[float]]) -> tuple[float, float]:
    entity, sub = split(sketch, ref)
    if entity is None:
        return 0.0, 0.0
    v = p[entity.id]
    if entity.kind == "line":
        return (v[0], v[1]) if sub == "start" else (v[2], v[3])
    if entity.kind == "arc" and sub in ("start", "end"):
        angle = v[3] if sub == "start" else v[4]
        return v[0] + v[2] * math.cos(angle), v[1] + v[2] * math.sin(angle)
    return v[0], v[1]


def _direction(v: list[float]) -> tuple[float, float]:
    dx, dy = v[2] - v[0], v[3] - v[1]
    size = math.hypot(dx, dy) or 1.0
    return dx / size, dy / size


def _size(entity: Entity, v: list[float]) -> float:
    return math.hypot(v[2] - v[0], v[3] - v[1]) if entity.kind == "line" else v[2]


def _residual(sketch: Sketch, c: Constraint, p: dict[str, list[float]]) -> list[float]:
    """How far the shapes are from satisfying one constraint: one number per movement it removes."""
    pt = lambda ref: _point(sketch, ref, p)  # noqa: E731
    k = c.kind
    if k == "coincident":
        a, b = c.refs
        if is_point(sketch, a) and is_point(sketch, b):
            (ax, ay), (bx, by) = pt(a), pt(b)
            return [ax - bx, ay - by]
        point, curve = (a, b) if is_point(sketch, a) else (b, a)
        (x, y), entity = pt(point), sketch.entities[curve]
        v = p[curve]
        if entity.kind == "line":
            dx, dy = _direction(v)
            return [(x - v[0]) * dy - (y - v[1]) * dx]
        return [math.hypot(x - v[0], y - v[1]) - v[2]]
    if k in ("horizontal", "vertical"):
        axis = 1 if k == "horizontal" else 0
        if len(c.refs) == 1:
            v = p[c.refs[0]]
            return [v[2 + axis] - v[axis]]
        return [pt(c.refs[0])[axis] - pt(c.refs[1])[axis]]
    if k in ("parallel", "perpendicular"):
        (ax, ay), (bx, by) = _direction(p[c.refs[0]]), _direction(p[c.refs[1]])
        return [ax * by - ay * bx] if k == "parallel" else [ax * bx + ay * by]
    if k == "equal":
        a, b = (sketch.entities[r] for r in c.refs)
        return [_size(a, p[a.id]) - _size(b, p[b.id])]
    if k == "midpoint":
        (mx, my), (ax, ay), (bx, by) = (pt(r) for r in c.refs)
        return [mx - (ax + bx) / 2, my - (ay + by) / 2]
    if k in ("diameter", "radius"):
        return [p[c.refs[0]][2] * (2 if k == "diameter" else 1) - c.value]
    if k == "length":
        v = p[c.refs[0]]
        return [math.hypot(v[2] - v[0], v[3] - v[1]) - c.value]
    if k == "gap":  # only met in sketches read from a CAD system: point to line, or between parallel lines
        a, b = c.refs
        x, y = pt(a) if is_point(sketch, a) else p[a][:2]
        v = p[b]
        dx, dy = _direction(v)
        return [abs((x - v[0]) * dy - (y - v[1]) * dx) - c.value]
    (ax, ay), (bx, by) = pt(c.refs[0]), pt(c.refs[1])
    if k == "distance":
        return [math.hypot(bx - ax, by - ay) - c.value]
    return [abs(bx - ax) - c.value] if k == "distance_x" else [abs(by - ay) - c.value]


def _all(sketch: Sketch, p: dict[str, list[float]]) -> list[float]:
    return [r for c in sketch.constraints for r in _residual(sketch, c, p)]


def _jacobian(sketch: Sketch, p: dict[str, list[float]], keys: list[tuple[str, int]]) -> list[list[float]]:
    base = _all(sketch, p)
    columns = []
    step = 1e-6
    for entity, i in keys:
        saved = p[entity][i]
        p[entity][i] = saved + step
        up = _all(sketch, p)
        p[entity][i] = saved - step
        down = _all(sketch, p)
        p[entity][i] = saved
        columns.append([(u - d) / (2 * step) for u, d in zip(up, down)])
    return [[columns[j][i] for j in range(len(keys))] for i in range(len(base))]


def _solve_square(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-300:
            continue
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-300 else 0.0 for i in range(n)]


def _solve(sketch: Sketch, p: dict[str, list[float]], keys: list[tuple[str, int]]) -> bool:
    """Move the shapes the least amount that satisfies every constraint (damped Gauss-Newton)."""
    for _ in range(30):
        r = _all(sketch, p)
        if max(map(abs, r), default=0.0) < SOLVED:
            return True
        j = _jacobian(sketch, p, keys)
        jjt = [[sum(x * y for x, y in zip(row_a, row_b)) for row_b in j] for row_a in j]
        damping = 1e-10 * max(jjt[i][i] for i in range(len(jjt)))
        for i in range(len(jjt)):
            jjt[i][i] += damping
        weights = _solve_square(jjt, [-x for x in r])
        for column, (entity, i) in enumerate(keys):
            p[entity][i] += sum(j[row][column] * weights[row] for row in range(len(r)))
    return max(map(abs, _all(sketch, p)), default=0.0) < SOLVED


def check(sketch: Sketch, strict: bool = True) -> Gate:
    """Run the gate. On success the sketch's shapes hold the exact solved coordinates.

    ``strict`` is for sketches about to be sent: a drawing far from its own dimensions is refused. For a sketch
    read back from a CAD system it is off, because stored coordinates may lag behind the dimensions.
    """
    gate = Gate()
    p = {e.id: e.params[:] for e in sketch.entities.values()}
    keys = [(e.id, i) for e in sketch.entities.values() for i in range(len(e.params))]
    for c in sketch.constraints:
        error = max(map(abs, _residual(sketch, c, p)))
        if strict and error > MAX_START_ERROR:
            gate.off.append((c.text(), error))
    if gate.off:
        return gate
    if not _solve(sketch, p, keys):
        gate.unsolved = True
        return gate
    for e in sketch.entities.values():  # angles count as the distance they sweep at the arc's radius
        for i, (a, b) in enumerate(zip(e.params, p[e.id])):
            gate.moved = max(gate.moved, abs(a - b) * (e.params[2] if e.kind == "arc" and i > 2 else 1.0))
    if strict and gate.moved > MAX_START_ERROR:
        gate.off.append(("the drawing as a whole (it would have to move this far to satisfy them all)", gate.moved))
        return gate

    # Rank by orthogonalising the constraint rows one at a time: a row that is already spanned adds nothing.
    jacobian = _jacobian(sketch, p, keys)
    basis: list[list[float]] = []
    row = 0
    for c in sketch.constraints:
        count, new = len(_residual(sketch, c, p)), 0
        for _ in range(count):
            v = jacobian[row][:]
            scale = math.sqrt(sum(x * x for x in v)) or 1.0
            for q in basis:
                dot = sum(x * y for x, y in zip(v, q))
                v = [x - dot * y for x, y in zip(v, q)]
            norm = math.sqrt(sum(x * x for x in v))
            if norm > 1e-6 * scale:
                basis.append([x / norm for x in v])
                new += 1
            row += 1
        if new < count:
            gate.repeated.append(c.text())
    gate.free = len(keys) - len(basis)
    if gate.free:
        for column, (entity, i) in enumerate(keys):  # a coordinate can move if the constraints do not span it
            if 1.0 - sum(q[column] ** 2 for q in basis) > 1e-6:
                gate.moving.setdefault(f"{sketch.entities[entity].kind} '{entity}'", []).append(PARAMS[sketch.entities[entity].kind][i])
    if gate.passed or not strict:
        for e in sketch.entities.values():
            e.params = p[e.id]
    return gate
