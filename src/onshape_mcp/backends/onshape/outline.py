"""Turn Onshape's feature-list JSON into text a model, or a person, can take in at a glance.

Onshape returns every parameter a feature could have (an extrude has about 45), most of them inactive defaults.
KEY_PARAMS names the ones a person would actually see in the dialog:

    "depth"                 always shown
    "symmetric?"            a switch, shown only when it is on
    "hasDraft>draftAngle"   draftAngle, shown only when the hasDraft switch is on
    "endBound=BLIND>depth"  depth, shown only when endBound is BLIND
"""
from __future__ import annotations

import math

from ...expr import ExpressionError, Quantity, evaluate
from ..base import FeatureOutline, Snapshot

KEY_PARAMS: dict[str, list[str]] = {
    "extrude": [
        "operationType", "endBound", "endBound=BLIND>depth", "symmetric?", "oppositeDirection?",
        "hasDraft>draftAngle", "hasSecondDirection>secondDirectionDepth",
        "startOffset>startOffsetDistance", "hasOffset>offsetDistance",
    ],
    "revolve": ["operationType", "fullRevolve?", "revolveType", "angle"],
    "fillet": ["radius", "isAsymmetric>otherRadius"],
    "chamfer": ["chamferType", "width", "angle"],
    "booleanBodies": ["operationType", "keepTools?"],
    "transform": [
        "transformType", "transformType=ROTATION>angle", "transformType=TRANSLATION_DISTANCE>distance",
        "transformType=TRANSLATION_3D>dx", "transformType=TRANSLATION_3D>dy", "transformType=TRANSLATION_3D>dz",
        "oppositeDirection?", "makeCopy?",
    ],
    "mirror": ["patternType", "operationType"],
    "circularPattern": ["patternType", "instanceCount", "angle", "equalSpace?"],
    "linearPattern": ["patternType", "instanceCount", "distance"],
    "cPlane": ["cplaneType", "offset"],
    "shell": ["thickness"],
    "mateConnector": ["originType", "entityInferenceType", "transform>translationX", "transform>translationY", "transform>translationZ"],
    "importDerived": ["placement"],
}
KIND = {"newSketch": "sketch", "assignVariable": "variable", "booleanBodies": "boolean", "cPlane": "plane",
        "importDerived": "derived"}
DIMENSION_IDS = ("length", "angle")
MAX_UNKNOWN = 8  # for feature types not in KEY_PARAMS


def variables(snapshot: Snapshot) -> dict[str, Quantity]:
    """The values of this tab's variables, in order of definition."""
    known: dict[str, Quantity] = {}
    for feature in snapshot.data.get("features", []):
        if feature.get("featureType") == "assignVariable" and not feature.get("suppressed"):
            for label, parameter in editable(feature):
                try:
                    known[label.lstrip("#")] = evaluate(parameter.get("expression", ""), known)
                except ExpressionError:
                    pass  # written with something we do not read; expressions that use it are not checked
    return known


def outline(snapshot: Snapshot) -> list[FeatureOutline]:
    states = snapshot.data.get("featureStates", {})
    known = variables(snapshot)
    out = []
    for index, feature in enumerate(snapshot.data.get("features", []), start=1):
        kind = feature.get("featureType", "?")
        item = FeatureOutline(
            index=index,
            id=feature.get("featureId", ""),
            kind=KIND.get(kind, kind),
            name=feature.get("name", ""),
            status=states.get(feature.get("featureId"), {}).get("featureStatus", "UNKNOWN"),
            suppressed=bool(feature.get("suppressed")),
        )
        if kind == "newSketch":
            _sketch(feature, item, known, snapshot.data.get("features", []))
        elif kind == "assignVariable":
            _variable(feature, item)
        else:
            _parameters(feature, item)
        out.append(item)
    return out


def shown_name(feature: dict) -> str:
    """A variable is listed as "#name", whatever Onshape stores as its feature name."""
    if feature.get("featureType") == "assignVariable":
        numbers = editable(feature)
        return numbers[0][0] if numbers else feature.get("name", "")
    return feature.get("name", "")


def find_feature(snapshot: Snapshot, selector: str) -> tuple[int, dict]:
    """By 1-based position, feature id, or name (exact, then unique partial match)."""
    features = snapshot.data.get("features", [])
    text = selector.strip()
    if text.isdigit() and 1 <= int(text) <= len(features):
        return int(text), features[int(text) - 1]
    for wanted in (lambda f: f.get("featureId") == text, lambda f: shown_name(f) == text,
                   lambda f: text.lower() in shown_name(f).lower()):
        hits = [(i, f) for i, f in enumerate(features, start=1) if wanted(f)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(f"{i} \"{shown_name(f)}\"" for i, f in hits)
            raise LookupError(f"'{selector}' matches several features: {names}. Use the position number.")
    raise LookupError(f"No feature '{selector}'. There are {len(features)} features; use a position, id or name.")


def describe(snapshot: Snapshot, selector: str) -> str:
    index, feature = find_feature(snapshot, selector)
    status = snapshot.data.get("featureStates", {}).get(feature.get("featureId"), {}).get("featureStatus", "UNKNOWN")
    lines = [f"{index}  {feature.get('featureType')}  \"{feature.get('name')}\"  [{status}]  id {feature.get('featureId')}"]
    if feature.get("featureType") == "newSketch":
        labels = _labels(feature)
        lines.append(f"plane: {_plane(feature, snapshot.data.get('features', []))}")
        lines += ["health: " + sketch_health(feature, variables(snapshot))[0]] + \
                 ["  can still move: " + line for line in sketch_health(feature, variables(snapshot))[1:]]
        lines.append("entities (mm):")
        for entity in feature.get("entities", []):
            lines.append(f"  {labels[entity['entityId']]:<10} {_geometry(entity)}"
                         + ("  construction" if entity.get("isConstruction") else ""))
        lines.append("constraints:")
        for constraint in feature.get("constraints", []):
            refs, value = _constraint(constraint, labels)
            lines.append(f"  {_constraint_name(constraint)}({', '.join(refs)})" + (f" = {value}" if value else ""))
    else:
        lines.append("parameters (everything Onshape stores, including inactive defaults):")
        for p in feature.get("parameters", []):
            lines.append(f"  {p.get('parameterId')} = {_value(p)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- sketches

def _kind(entity: dict) -> str:
    kind, geometry = entity.get("btType", ""), entity.get("geometry", {}).get("btType", "")
    if kind.startswith("BTMSketchPoint"):
        return "point"
    if "Circle" in geometry:
        return "circle" if kind.startswith("BTMSketchCurve-") else "arc"
    if "Line" in geometry:
        return "line"
    return geometry.removeprefix("BTCurveGeometry").split("-")[0].lower() or "entity"


def _labels(feature: dict) -> dict[str, str]:
    """Onshape's entity ids are opaque ("YHzoF1XJnGLP"); number them by kind instead ("circle 2")."""
    counts: dict[str, int] = {}
    labels = {}
    for entity in feature.get("entities", []):
        kind = _kind(entity)
        counts[kind] = counts.get(kind, 0) + 1
        labels[entity["entityId"]] = f"{kind} {counts[kind]}"
    return labels


def _ref(ref: str, labels: dict[str, str]) -> str:
    best = max((eid for eid in labels if ref == eid or ref.startswith(eid + ".")), key=len, default=None)
    return labels[best] + ref[len(best):] if best else ref


def _constraint(constraint: dict, labels: dict[str, str]) -> tuple[list[str], str | None]:
    refs, value = [], None
    for p in constraint.get("parameters", []):
        pid = p.get("parameterId", "")
        if pid in DIMENSION_IDS and p.get("btType", "").startswith("BTMParameterQuantity"):
            value = p.get("expression")
        elif pid.startswith("local") and isinstance(p.get("value"), str):
            refs.append(_ref(p["value"], labels))
        elif pid.startswith("external"):
            refs.append("outside geometry")
    return refs, value


def _constraint_name(constraint: dict) -> str:
    """The same words the sketch builder uses: a distance measured along one axis is distance_x or distance_y."""
    name = constraint.get("constraintType", "?").lower()
    if name == "distance":
        direction = next((p.get("value") for p in constraint.get("parameters", []) if p.get("parameterId") == "direction"), None)
        name += {"HORIZONTAL": "_x", "VERTICAL": "_y"}.get(direction, "")
    return name


def editable(feature: dict) -> list[tuple[str, dict]]:
    """Every number of a feature as (label, the parameter that holds it), so a caller can read or change it."""
    kind = feature.get("featureType")
    if kind == "newSketch":
        labels, out = _labels(feature), []
        for constraint in feature.get("constraints", []):
            refs, _ = _constraint(constraint, labels)
            for p in constraint.get("parameters", []):
                if p.get("parameterId") in DIMENSION_IDS and p.get("btType", "").startswith("BTMParameterQuantity"):
                    out.append((f"{_constraint_name(constraint)}({', '.join(refs)})", p))
        return out
    params = {p.get("parameterId"): p for p in feature.get("parameters", [])}
    if kind == "assignVariable":
        variable_type = params.get("variableType", {}).get("value", "ANY")
        holder = {"LENGTH": "lengthValue", "ANGLE": "angleValue", "NUMBER": "numberValue"}.get(variable_type, "anyValue")
        return [(f"#{params.get('name', {}).get('value', '?')}", params[holder])] if holder in params else []
    return [(pid, p) for pid, p in params.items() if p.get("btType", "").startswith("BTMParameterQuantity")]


def base_plane(feature: dict) -> str | None:
    """Top, Front or Right when the sketch is drawn directly on one of them."""
    for p in feature.get("parameters", []):
        if p.get("parameterId") == "sketchPlane":
            query = " ".join(q.get("queryString", "") for q in p.get("queries", []))
            for name in ("Top", "Front", "Right"):
                if f'makeId("{name}")' in query or f"{name}planeOp" in query:
                    return name
    return None


def _plane(feature: dict, features: list[dict] = ()) -> str:
    """Where a sketch is drawn, in words: 'the Top plane', 'the plane "Post base"', or 'a face or custom plane'."""
    base = base_plane(feature)
    if base:
        return f"the {base} plane"
    query = " ".join(q.get("queryString", "") for p in feature.get("parameters", []) if p.get("parameterId") == "sketchPlane"
                     for q in p.get("queries", []))
    for other in features:
        if other.get("featureType") == "cPlane" and other.get("featureId") and other["featureId"] in query:
            return f"the plane \"{other.get('name')}\""
    return "a face or custom plane"


def _geometry(entity: dict) -> str:
    g = entity.get("geometry", {})
    mm = lambda v: f"{v * 1000:.3f}".rstrip("0").rstrip(".")  # noqa: E731
    kind = _kind(entity)
    if kind == "circle":
        return f"centre ({mm(g['xCenter'])}, {mm(g['yCenter'])}) radius {mm(g['radius'])}"
    if kind == "arc":
        a0, a1 = math.degrees(entity.get("startParam", 0)), math.degrees(entity.get("endParam", 0))
        return f"centre ({mm(g['xCenter'])}, {mm(g['yCenter'])}) radius {mm(g['radius'])} from {a0:.1f} to {a1:.1f} deg"
    if kind == "line":
        point = lambda t: (g["pntX"] + g["dirX"] * t, g["pntY"] + g["dirY"] * t)  # noqa: E731
        (x0, y0), (x1, y1) = point(entity.get("startParam", 0)), point(entity.get("endParam", 0))
        return f"({mm(x0)}, {mm(y0)}) to ({mm(x1)}, {mm(y1)})"
    if kind == "point":
        return f"({mm(entity.get('x', 0))}, {mm(entity.get('y', 0))})" if "x" in entity else ""
    return ""


def sketch_health(feature: dict, known: dict[str, Quantity]) -> list[str]:
    """Whether the sketch is fully defined, in the words and shape names the outline uses."""
    from . import sketch_io  # here, not at the top: sketch_io builds on this module's neighbours

    text, gate = sketch_io.health(feature, known)
    lines = [text]
    if gate is not None and gate.free:
        labels = _labels(feature)
        for entity, coordinates in gate.moving.items():
            name = entity.split("'")[1]
            lines.append(f"{labels.get(name, name)}: {', '.join(coordinates)}")
    return lines


def _sketch(feature: dict, item: FeatureOutline, known: dict[str, Quantity], features: list[dict]) -> None:
    labels = _labels(feature)
    counts: dict[str, int] = {}
    for label in labels.values():
        kind = label.split()[0]
        counts[kind] = counts.get(kind, 0) + 1
    construction = sum(1 for e in feature.get("entities", []) if e.get("isConstruction"))
    shapes = ", ".join(f"{n} {kind}{'s' if n > 1 else ''}" for kind, n in counts.items()) or "empty"
    item.notes.append(f"on {_plane(feature, features)}: {shapes}" + (f" ({construction} construction)" if construction else "")
                      + f", {len(feature.get('constraints', []))} constraints")
    health = sketch_health(feature, known)
    item.notes.append(health[0] + (" (" + "; ".join(health[1:4]) + ("; ..." if len(health) > 4 else "") + ")" if len(health) > 1 else ""))
    item.numbers += [(label, p.get("expression")) for label, p in editable(feature)]
    hidden = sum(1 for c in feature.get("constraints", [])
                 if any(p.get("parameterId") in DIMENSION_IDS for p in c.get("parameters", []))
                 and not any(p.get("parameterId", "").startswith("label") for p in c.get("parameters", [])))
    if hidden:
        item.notes.append(f"{hidden} dimension{'s have' if hidden > 1 else ' has'} no label, so the sketch editor may not show "
                          f"{'them' if hidden > 1 else 'it'}")


# ---------------------------------------------------------------- other features

def _value(p: dict) -> str:
    kind = p.get("btType", "")
    if kind.startswith("BTMParameterQueryList"):
        n = len(p.get("queries", []))
        return f"<{n} selection{'s' if n != 1 else ''}>"
    if "expression" in p:
        return str(p["expression"])
    return str(p.get("value"))


def _variable(feature: dict, item: FeatureOutline) -> None:
    for label, p in editable(feature):
        item.name = label
        item.numbers.append((label, _value(p)))


def _parameters(feature: dict, item: FeatureOutline) -> None:
    params = {p.get("parameterId"): p for p in feature.get("parameters", [])}
    spec = KEY_PARAMS.get(feature.get("featureType"))
    if spec is None:  # an unknown or custom feature: show its numbers and choices, capped
        shown = [p for p in feature.get("parameters", [])
                 if p.get("btType", "").startswith(("BTMParameterQuantity", "BTMParameterEnum"))]
        for p in shown[:MAX_UNKNOWN]:
            item.numbers.append((p["parameterId"], _value(p)))
        if len(shown) > MAX_UNKNOWN:
            item.notes.append(f"{len(shown) - MAX_UNKNOWN} more parameters: use inspect")
        return
    for entry in spec:
        if entry.endswith("?"):
            if params.get(entry[:-1], {}).get("value") is True:
                item.notes.append(entry[:-1])
            continue
        condition, _, pid = entry.rpartition(">")
        if condition:
            switch, _, wanted = condition.partition("=")
            actual = params.get(switch, {}).get("value")
            if (actual != wanted) if wanted else (actual is not True):
                continue
        if pid not in params:
            continue
        if params[pid].get("btType", "").startswith("BTMParameterEnum"):
            item.notes.append(str(params[pid].get("value")))
        else:
            item.numbers.append((pid, _value(params[pid])))
