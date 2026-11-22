"""Read an existing Onshape sketch into the neutral Sketch, so it can be judged and re-solved here.

Only sketches made entirely of things the gate understands are read. Anything else (tangent, offset, mirror,
splines, references to other parts) is reported as "not assessed" with the reason, never guessed at.
"""
from __future__ import annotations

import math

from ...expr import ExpressionError, Quantity, evaluate
from ...sketch import ORIGIN, Constraint, Entity, Gate, Sketch, SketchError, check, is_point, split
from .build import ORIGIN_VERTEX

SAME_NAME = {"COINCIDENT": "coincident", "HORIZONTAL": "horizontal", "VERTICAL": "vertical", "PARALLEL": "parallel",
             "PERPENDICULAR": "perpendicular", "EQUAL": "equal", "DIAMETER": "diameter", "RADIUS": "radius", "LENGTH": "length"}
LOCAL_ORDER = ("localMidpoint", "localFirst", "localEntity1", "localSecond", "localEntity2")


def _entity(e: dict) -> Entity | str:
    kind, g = e.get("btType", ""), e.get("geometry", {})
    shape = g.get("btType", "")
    construction = bool(e.get("isConstruction"))
    if kind.startswith("BTMSketchPoint"):
        return Entity(e["entityId"], "point", [e.get("x", 0.0) * 1000, e.get("y", 0.0) * 1000], True)
    if "Line" in shape:
        start, end = e.get("startParam", 0.0), e.get("endParam", 0.0)
        return Entity(e["entityId"], "line", [(g["pntX"] + g["dirX"] * start) * 1000, (g["pntY"] + g["dirY"] * start) * 1000,
                                              (g["pntX"] + g["dirX"] * end) * 1000, (g["pntY"] + g["dirY"] * end) * 1000], construction)
    if "Circle" in shape:
        centre = [g["xCenter"] * 1000, g["yCenter"] * 1000, g["radius"] * 1000]
        if kind.startswith("BTMSketchCurve-"):
            return Entity(e["entityId"], "circle", centre, construction)
        if g.get("clockwise"):
            return "a clockwise arc"
        turn = math.atan2(g.get("yDir", 0.0), g.get("xDir", 1.0))
        first, last = e.get("startParam", 0.0) + turn, e.get("endParam", 0.0) + turn
        while last <= first:
            last += 2 * math.pi
        return Entity(e["entityId"], "arc", centre + [first, last], construction)
    return "a " + (shape.removeprefix("BTCurveGeometry").split("-")[0].lower() or "shape of an unknown kind")


def _is_origin(parameter: dict) -> bool:
    return any(q.get("deterministicIds") == [ORIGIN_VERTEX] for q in parameter.get("queries", []))


def to_sketch(feature: dict, variables: dict[str, Quantity]) -> tuple[Sketch | None, list[str]]:
    """(the sketch, []) or (None, the reasons it cannot be read)."""
    reasons: list[str] = []
    entities: dict[str, Entity] = {}
    for raw in feature.get("entities", []):
        made = _entity(raw)
        if isinstance(made, str):
            reasons.append(made)
        else:
            entities[made.id] = made
    # A point pinned to the Part Studio origin stands for the origin itself.
    alias: dict[str, str] = {}
    pins = set()
    for i, c in enumerate(feature.get("constraints", [])):
        parameters = c.get("parameters", [])
        outside = [p for p in parameters if p.get("parameterId", "").startswith("external")]
        local = [p.get("value") for p in parameters if p.get("parameterId", "").startswith("local")]
        if c.get("constraintType") == "COINCIDENT" and len(outside) == 1 and _is_origin(outside[0]) and len(local) == 1 \
                and local[0] in entities and entities[local[0]].kind == "point":
            alias[local[0]] = ORIGIN
            pins.add(i)
    for name in alias:
        del entities[name]
    sketch = Sketch(feature.get("name", ""), "Top", entities, [])

    def ref(text: str) -> str:
        return alias.get(text, text)

    for i, c in enumerate(feature.get("constraints", [])):
        if i in pins:
            continue
        kind = c.get("constraintType", "?")
        parameters = {p.get("parameterId"): p for p in c.get("parameters", [])}
        if parameters.get("driven", {}).get("value") is True:
            continue  # a driven dimension only displays a value
        refs = [ref(parameters[k]["value"]) for k in LOCAL_ORDER if k in parameters and isinstance(parameters[k].get("value"), str)]
        outside = [p for k, p in parameters.items() if k.startswith("external")]
        if outside:
            if not all(_is_origin(p) for p in outside):
                reasons.append("geometry outside the sketch")
                continue
            refs = ([ORIGIN] + refs) if "externalFirst" in parameters else (refs + [ORIGIN])
        try:
            for r in refs:
                split(sketch, r)
            points = [is_point(sketch, r) for r in refs]
            value = expression = None
            if "length" in parameters:
                expression = parameters["length"].get("expression", "")
                quantity = evaluate(expression, variables)
                if quantity.kind != "length":
                    raise ExpressionError("no unit")
                value = quantity.value
            if kind in SAME_NAME:
                name = SAME_NAME[kind]
            elif kind == "CONCENTRIC":
                name, refs = "coincident", [r + ".center" for r in refs]
            elif kind == "MIDPOINT":
                name = "midpoint"
                if len(refs) == 2:  # a point and a line
                    point, line = (refs[0], refs[1]) if points[0] else (refs[1], refs[0])
                    refs = [point, line + ".start", line + ".end"]
            elif kind == "DISTANCE":
                direction = parameters.get("direction", {}).get("value")
                if direction in ("HORIZONTAL", "VERTICAL") and all(points):
                    name = "distance_x" if direction == "HORIZONTAL" else "distance_y"
                elif all(points):
                    name = "distance"
                elif all(r == ORIGIN or points[n] or split(sketch, r)[0].kind == "line" for n, r in enumerate(refs)) and not points[-1]:
                    name = "gap"
                elif points[-1] and not points[0] and split(sketch, refs[0])[0].kind == "line":
                    name, refs = "gap", [refs[1], refs[0]]
                else:
                    reasons.append("a distance to a circle or arc")
                    continue
            else:
                reasons.append(f"a {kind.lower().replace('_', ' ')} constraint")
                continue
            sketch.constraints.append(Constraint(name, refs, expression, value))
        except SketchError:
            reasons.append("a reference this tool cannot follow")
        except ExpressionError:
            reasons.append("a value this tool cannot read here")
    return (None, sorted(set(reasons))) if reasons else (sketch, [])


def health(feature: dict, variables: dict[str, Quantity]) -> tuple[str, Gate | None]:
    """One line a person can act on, and the gate result when the sketch could be assessed."""
    sketch, reasons = to_sketch(feature, variables)
    if sketch is None:
        return "not assessed here (it uses " + ", ".join(reasons) + ")", None
    if not sketch.entities:
        return "empty", None
    gate = check(sketch, strict=False)
    if gate.unsolved:
        return "its constraints cannot all hold at once", gate
    text = f"NOT fully defined: {gate.free} movement{'s' if gate.free > 1 else ''} still free" if gate.free else "fully defined"
    if gate.repeated:  # the CAD system accepts these as long as they agree, which they do if we got this far
        text += f"; {len(gate.repeated)} constraint{'s repeat' if len(gate.repeated) > 1 else ' repeats'} others"
    return text, gate


def write_back(feature: dict, sketch: Sketch) -> float:
    """Put solved coordinates into the Onshape sketch. Returns the largest change, in mm."""
    moved = 0.0
    for raw in feature.get("entities", []):
        entity = sketch.entities.get(raw.get("entityId"))
        if entity is None:
            continue
        before = _entity(raw)
        moved = max([moved] + [abs(a - b) for a, b in zip(before.params, entity.params)])
        v, g = entity.params, raw.get("geometry", {})
        if entity.kind == "point":
            raw["x"], raw["y"] = v[0] / 1000, v[1] / 1000
        elif entity.kind == "line":
            size = math.hypot(v[2] - v[0], v[3] - v[1])
            g.update(pntX=v[0] / 1000, pntY=v[1] / 1000, dirX=(v[2] - v[0]) / size, dirY=(v[3] - v[1]) / size)
            raw["startParam"], raw["endParam"] = 0.0, size / 1000
        else:
            g.update(xCenter=v[0] / 1000, yCenter=v[1] / 1000, radius=v[2] / 1000)
            if entity.kind == "arc":
                g.update(xDir=1, yDir=0, clockwise=False)
                raw["startParam"], raw["endParam"] = v[3], v[4]
    return moved
