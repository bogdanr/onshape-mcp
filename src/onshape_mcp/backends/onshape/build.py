"""Onshape feature JSON for what the tools build: variables, sketches, extrudes.

Every shape here was copied from features that work in real documents. Two details decide whether a person can
use the result in the sketch editor, and both are easy to get wrong:

* every constraint needs its own ``entityId``, or its symbol is not drawn;
* every dimension needs label parameters, or it constrains the sketch but may show no number to click on.
"""
from __future__ import annotations

import math

from ...sketch import ORIGIN, Constraint, Entity, Sketch, split

ORIGIN_VERTEX = "IB"  # the Part Studio origin's deterministic id
# sketch (u, v) -> world (x, y, z), and each plane's normal: the way an offset plane moves and a plain extrude goes
PLANE_AXES = {"Top": ((1, 0, 0), (0, 1, 0)), "Front": ((1, 0, 0), (0, 0, 1)), "Right": ((0, 1, 0), (0, 0, 1))}
PLANE_NORMAL = {"Top": (0, 0, 1), "Front": (0, -1, 0), "Right": (1, 0, 0)}
NAME_PROPERTY = "57f3fb8efa3416c06701d60d"  # Onshape's built-in "Name" property
VARIABLE_VALUE = {"length": ("LENGTH", "lengthValue"), "angle": ("ANGLE", "angleValue"), "number": ("NUMBER", "numberValue")}
OPERATIONS = {"new": "NEW", "add": "ADD", "remove": "REMOVE"}


def _string(pid: str, value: str) -> dict:
    return {"btType": "BTMParameterString-149", "parameterId": pid, "value": value}


def _enum(pid: str, enum_name: str, value: str) -> dict:
    return {"btType": "BTMParameterEnum-145", "parameterId": pid, "enumName": enum_name, "value": value}


def _quantity(pid: str, expression: str) -> dict:
    return {"btType": "BTMParameterQuantity-147", "parameterId": pid, "expression": expression}


def _boolean(pid: str, value: bool) -> dict:
    return {"btType": "BTMParameterBoolean-144", "parameterId": pid, "value": value}


def _query(pid: str, *queries: dict) -> dict:
    return {"btType": "BTMParameterQueryList-148", "parameterId": pid, "queries": list(queries)}


def _fs(text: str) -> dict:
    return {"btType": "BTMIndividualQuery-138", "queryString": f"query={text};"}


def _feature(feature_type: str, name: str, parameters: list[dict]) -> dict:
    return {"btType": "BTMFeature-134", "featureType": feature_type, "name": name, "parameters": parameters}


# ---------------------------------------------------------------- variable

def variable(name: str, expression: str, kind: str) -> dict:
    variable_type, holder = VARIABLE_VALUE[kind]
    return _feature("assignVariable", "###name = #value", [  # Onshape's own template: shows as "#name = value"
        _enum("variableType", "VariableType", variable_type), _string("name", name), _quantity(holder, expression)])


# ---------------------------------------------------------------- sketch

def _m(mm: float) -> float:
    return mm / 1000.0


def _entity(e: Entity) -> dict:
    v = e.params
    if e.kind == "point":
        return {"btType": "BTMSketchPoint-158", "entityId": e.id, "isConstruction": True, "isUserPoint": True,
                "x": _m(v[0]), "y": _m(v[1])}
    if e.kind == "line":
        size = math.hypot(v[2] - v[0], v[3] - v[1])
        return {"btType": "BTMSketchCurveSegment-155", "entityId": e.id, "isConstruction": e.construction,
                "startPointId": f"{e.id}.start", "endPointId": f"{e.id}.end", "startParam": 0.0, "endParam": _m(size),
                "geometry": {"btType": "BTCurveGeometryLine-117", "pntX": _m(v[0]), "pntY": _m(v[1]),
                             "dirX": (v[2] - v[0]) / size, "dirY": (v[3] - v[1]) / size}}
    circle = {"btType": "BTCurveGeometryCircle-115", "radius": _m(v[2]), "xCenter": _m(v[0]), "yCenter": _m(v[1]),
              "xDir": 1, "yDir": 0, "clockwise": False}
    if e.kind == "circle":  # a full circle is its own type; drawing it as arcs makes it undimensionable
        return {"btType": "BTMSketchCurve-4", "entityId": e.id, "centerId": f"{e.id}.center",
                "isConstruction": e.construction, "geometry": circle}
    return {"btType": "BTMSketchCurveSegment-155", "entityId": e.id, "isConstruction": e.construction,
            "startPointId": f"{e.id}.start", "endPointId": f"{e.id}.end", "centerId": f"{e.id}.center",
            "startParam": v[3], "endParam": v[4], "geometry": circle}


def _labels(kind: str, n: int) -> list[dict]:
    """Where the dimension's number is drawn. Staggered so neighbours do not sit on top of each other."""
    if kind in ("diameter", "radius"):
        return [_quantity("labelRatio", "0.6"), _quantity("labelAngle", f"{0.5 + 0.7 * (n % 4):.2f}*rad")]
    return [_quantity("labelRatio", "0.5"), _quantity("labelDistance", f"{0.010 + 0.007 * (n % 3):.3f}*m")]


def _point_xy(sketch: Sketch, ref: str) -> tuple[float, float]:
    entity, sub = split(sketch, ref)
    if entity is None:
        return 0.0, 0.0
    v = entity.params
    if entity.kind == "line":
        return (v[0], v[1]) if sub == "start" else (v[2], v[3])
    if entity.kind == "arc" and sub in ("start", "end"):
        angle = v[3] if sub == "start" else v[4]
        return v[0] + v[2] * math.cos(angle), v[1] + v[2] * math.sin(angle)
    return v[0], v[1]


def _constraint(sketch: Sketch, c: Constraint, n: int) -> dict:
    kind, refs = c.kind, c.refs
    if kind == "midpoint":
        parameters = [_string("localMidpoint", refs[0]), _string("localEntity1", refs[1]), _string("localEntity2", refs[2])]
    else:
        parameters = [_string("localFirst", refs[0])] + ([_string("localSecond", refs[1])] if len(refs) > 1 else [])
    constraint_type = kind.upper()
    if kind in ("diameter", "radius"):
        parameters += [_quantity("length", c.expression)]
    elif kind == "length":
        parameters += [_enum("direction", "DimensionDirection", "MINIMUM"), _quantity("length", c.expression),
                       _enum("alignment", "DimensionAlignment", "ALIGNED")]
    elif kind == "distance":
        parameters += [_enum("direction", "DimensionDirection", "MINIMUM"), _quantity("length", c.expression)]
    elif kind in ("distance_x", "distance_y"):
        constraint_type = "DISTANCE"
        axis = 0 if kind == "distance_x" else 1
        ahead = _point_xy(sketch, refs[1])[axis] > _point_xy(sketch, refs[0])[axis]
        parameters += [_enum("direction", "DimensionDirection", "HORIZONTAL" if axis == 0 else "VERTICAL"),
                       _quantity("length", c.expression),
                       _enum("alignment", "DimensionAlignment", "ALIGNED" if ahead else "ANTI_ALIGNED")]
    if c.expression is not None:
        parameters += _labels(kind, n)
    return {"btType": "BTMSketchConstraint-2", "constraintType": constraint_type, "entityId": f"c{n}_{kind}",
            "parameters": parameters}


def labels_for(constraint_type: str, n: int) -> list[dict]:
    """Label parameters for an existing Onshape dimension that has none."""
    return _labels("diameter" if constraint_type in ("DIAMETER", "RADIUS") else "length", n)


def plane(name: str, base: str, offset: str, flip: bool = False) -> dict:
    return _feature("cPlane", name, [
        _query("entities", _fs(f'qCreatedBy(makeId("{base}"), EntityType.FACE)')),
        _enum("cplaneType", "CPlaneType", "OFFSET"), _quantity("offset", offset), _boolean("oppositeDirection", flip)])


def sketch(sk: Sketch, plane_id: str | None = None) -> dict:
    """``plane_id`` is the feature id of an offset plane; without it the sketch goes on the default plane sk.plane."""
    entities, constraints = [], []
    if sk.uses_origin():  # one point pinned to the real origin; everything else is dimensioned from it
        entities.append({"btType": "BTMSketchPoint-158", "entityId": ORIGIN, "isConstruction": True, "isUserPoint": True,
                         "x": 0.0, "y": 0.0})
        constraints.append({"btType": "BTMSketchConstraint-2", "constraintType": "COINCIDENT", "entityId": "origin_pin",
                            "parameters": [_string("localFirst", ORIGIN),
                                           _query("externalSecond", {"btType": "BTMIndividualQuery-138",
                                                                     "deterministicIds": [ORIGIN_VERTEX]})]})
    entities += [_entity(e) for e in sk.entities.values()]
    constraints += [_constraint(sk, c, n) for n, c in enumerate(sk.constraints, start=1)]
    return {"btType": "BTMSketch-151", "featureType": "newSketch", "name": sk.name,
            "parameters": [_query("sketchPlane", _fs(f'qCreatedBy(makeId("{plane_id or sk.plane}"), EntityType.FACE)'))],
            "entities": entities, "constraints": constraints}


# ---------------------------------------------------------------- extrude

def world(plane: str, u: float, v: float, offset: float = 0.0) -> tuple[float, float, float]:
    """A sketch point in model space, in millimetres. ``offset`` is how far the sketch's plane is from ``plane``."""
    eu, ev = PLANE_AXES[plane]
    return tuple(u * a + v * b + offset * n for a, b, n in zip(eu, ev, PLANE_NORMAL[plane]))


def _vector(point) -> str:
    return "vector(" + ", ".join(f"{c / 1000:.7f}" for c in point) + ") * meter"


def edges(points) -> dict:
    """The edge nearest to each point, so a pick survives changes that renumber the model's edges."""
    return _fs("qUnion([" + ", ".join(
        f"qClosestTo(qOwnedByBody(qAllModifiableSolidBodies(), EntityType.EDGE), {_vector(p)})" for p in points) + "])")


def fillet(name: str, points, radius: str) -> dict:
    return _feature("fillet", name, [_query("entities", edges(points)), _quantity("radius", radius)])


def chamfer(name: str, points, width: str) -> dict:
    return _feature("chamfer", name, [_query("entities", edges(points)), _enum("chamferType", "ChamferType", "EQUAL_OFFSETS"),
                                      _quantity("width", width)])


def extrude(name: str, sketch_id: str, plane: str, regions, depth: str, operation: str = "new",
            flip: bool = False, symmetric: bool = False, offset: float = 0.0) -> dict:
    if regions == "all":  # the outer outline, with every closed shape inside it left as a hole
        picked = {"btType": "BTMIndividualSketchRegionQuery-140", "featureId": sketch_id, "filterInnerLoops": True}
    else:
        points = [world(plane, u, v, offset) for u, v in regions]
        picked = _fs("qUnion([" + ", ".join(
            f'qClosestTo(qSketchRegion(makeId("{sketch_id}"), false), vector({x / 1000:.7f}, {y / 1000:.7f}, {z / 1000:.7f}) * meter)'
            for x, y, z in points) + "])")
    return _feature("extrude", name, [
        _enum("bodyType", "ExtendedToolBodyType", "SOLID"),
        _enum("operationType", "NewBodyOperationType", OPERATIONS[operation]),
        _query("entities", picked),
        _enum("endBound", "BoundingType", "BLIND"),
        _quantity("depth", depth),
        _boolean("oppositeDirection", flip),
        _boolean("symmetric", symmetric),
        _boolean("defaultScope", True),
    ])
