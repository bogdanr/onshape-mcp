"""Onshape as a CAD backend."""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import quote

from ...budget import Ledger
from ...config import ConfigError
from ...expr import ExpressionError, Quantity, UnknownVariable, evaluate, length
from ...motion import Assembly, Instance, Joint, transform_direction, transform_point
from ...sketch import check
from ..base import CadBackend, ChangePlan, FeatureOutline, ModelRef, Snapshot, WriteResult
from . import build, sketch_io
from . import outline as _outline
from .client import DEFAULT_BASE_URL, OnshapeClient, OnshapeError, load_credentials

URL = re.compile(r"/documents/(?P<did>[0-9a-f]{24})/(?P<wvm>[wvm])/(?P<wvmid>[0-9a-f]{24})/e/(?P<eid>[0-9a-f]{24})")
STATUS_SCRIPT = """function(context is Context, queries is map) {
    var out = {};
    for (var fid in %s) { out[fid] = getFeatureStatus(context, makeId(fid)); }
    return out;
}"""
BODIES_SCRIPT = """function(context is Context, queries is map) {
    var out = {};
    for (var fid in %s) {
        var ids = [];
        for (var body in evaluateQuery(context, qBodyType(qCreatedBy(makeId(fid), EntityType.BODY), BodyType.SOLID))) {
            ids = append(ids, transientQueriesToStrings(body));
        }
        out[fid] = ids;
    }
    return out;
}"""
PARTS_SCRIPT = """function(context is Context, queries is map) {
    var out = [];
    for (var body in evaluateQuery(context, qAllModifiableSolidBodies())) {
        const bounds = evBox3d(context, { "topology" : body, "tight" : true });
        out = append(out, [transientQueriesToStrings(body), bounds.minCorner / meter, bounds.maxCorner / meter,
                           evVolume(context, { "entities" : body }) / meter ^ 3,
                           getProperty(context, { "entity" : body, "propertyType" : PropertyType.NAME })]);
    }
    return out;
}"""
CHANGED = ("The document changed after it was read, so nothing was changed. "
           "Read it again (cad_read) and repeat.")


class OnshapeBackend(CadBackend):
    name = "onshape"

    def __init__(self, client: OnshapeClient):
        self.client = client

    @classmethod
    def from_settings(cls, settings: dict, ledger: Ledger) -> "OnshapeBackend":
        find_keys = lambda: load_credentials(settings.get("credentials_file"))  # noqa: E731  on first use, not at start
        return cls(OnshapeClient(find_keys, ledger, settings.get("base_url", DEFAULT_BASE_URL)))

    def problem(self) -> str | None:
        try:
            self.client.credentials
        except ConfigError as e:
            return str(e)
        return None

    # ---------------------------------------------------------------- reading

    def parse_ref(self, text: str) -> ModelRef:
        found = URL.search(text)
        if not found:
            raise ValueError(
                "Not an Onshape tab link. Open the Part Studio tab and copy the address: "
                "https://cad.onshape.com/documents/<document>/w/<workspace>/e/<element>"
            )
        return ModelRef("onshape", found["did"], f"{found['wvm']}/{found['wvmid']}", found["eid"])

    def _studio(self, ref: ModelRef, writing: bool = False) -> str:
        if writing and not ref.workspace.startswith("w/"):
            raise ValueError("Only a workspace can be changed. Use the link of the live tab, not of a saved version.")
        return f"/partstudios/d/{ref.document}/{ref.workspace}/e/{ref.element}"

    def read_studio(self, ref: ModelRef) -> Snapshot:
        data = self.client.get_json(self._studio(ref) + "/features")
        return Snapshot(ref, data["sourceMicroversion"], data)

    def outline(self, snapshot: Snapshot) -> list[FeatureOutline]:
        return _outline.outline(snapshot)

    def describe_feature(self, snapshot: Snapshot, selector: str) -> str:
        return _outline.describe(snapshot, selector)

    def export_mesh(self, ref: ModelRef, version: str) -> bytes:
        # Pinned to the microversion the features were read at, so the mesh and the tree always match.
        return self.client.request(
            "GET",
            f"/partstudios/d/{ref.document}/m/{version}/e/{ref.element}/stl",
            query={"mode": "binary", "units": "millimeter", "grouping": "true"},
            accept="*/*",
        )

    def variables(self, snapshot: Snapshot) -> dict[str, Quantity]:
        return _outline.variables(snapshot)

    def feature_names(self, snapshot: Snapshot) -> dict[str, str]:
        return {_outline.shown_name(f): f.get("featureId", "") for f in snapshot.data.get("features", [])}

    def plane_info(self, snapshot: Snapshot, selector: str) -> tuple[str, str, float]:
        """(feature id, base plane, signed offset in mm) of a plane offset from Top, Front or Right."""
        _, feature = _outline.find_feature(snapshot, selector)
        return self._plane_of(snapshot, feature)

    def _plane_of(self, snapshot: Snapshot, feature: dict) -> tuple[str, str, float]:
        parameters = {p.get("parameterId"): p for p in feature.get("parameters", [])}
        picked = " ".join(q.get("queryString", "") for q in parameters.get("entities", {}).get("queries", []))
        base = next((name for name in build.PLANE_AXES if f'makeId("{name}")' in picked or f"{name}planeOp" in picked), None)
        if feature.get("featureType") != "cPlane" or parameters.get("cplaneType", {}).get("value") != "OFFSET" or base is None:
            raise ValueError(f"\"{feature.get('name')}\" is not a plane offset from Top, Front or Right, "
                             "which is the only kind this tool sketches on so far.")
        offset = length(parameters["offset"].get("expression", ""), self.variables(snapshot))
        return feature["featureId"], base, -offset if parameters.get("oppositeDirection", {}).get("value") else offset

    def sketch_plane(self, snapshot: Snapshot, selector: str) -> tuple[str, str, float]:
        _, feature = _outline.find_feature(snapshot, selector)
        if feature.get("featureType") != "newSketch":
            raise ValueError(f"\"{feature.get('name')}\" is not a sketch.")
        plane = _outline.base_plane(feature)
        if plane:
            return feature["featureId"], plane, 0.0
        query = " ".join(q.get("queryString", "") for p in feature.get("parameters", []) if p.get("parameterId") == "sketchPlane"
                         for q in p.get("queries", []))
        for other in snapshot.data.get("features", []):
            if other.get("featureType") == "cPlane" and other.get("featureId") and other["featureId"] in query:
                _, base, offset = self._plane_of(snapshot, other)
                return feature["featureId"], base, offset
        raise ValueError(f"Sketch \"{feature.get('name')}\" is on a face or a plane this tool cannot place, "
                         "so regions cannot be picked by point in it. Use regions \"all\".")

    # ---------------------------------------------------------------- writing: shared

    def _write(self, ref: ModelRef, path: str, body: dict, after: str | None) -> dict:
        """One write, refused by Onshape if the document moved on from the version we last saw."""
        if after:
            body = dict(body, sourceMicroversion=after, rejectMicroversionSkew=True)
        try:
            return self.client.post_json(self._studio(ref, writing=True) + path, body)
        except OnshapeError as e:
            if e.status == 409 or "skew" in e.message.lower():
                raise RuntimeError(CHANGED) from None
            raise

    # ---------------------------------------------------------------- changing numbers

    def plan_changes(self, snapshot: Snapshot, changes: list[dict]) -> ChangePlan:
        variables = self.variables(snapshot)
        touched: dict[str, dict] = {}  # feature id -> an edited copy of the whole feature
        lines, notes = [], []
        for change in changes:
            unknown = set(change) - {"feature", "number", "to", "was"}
            if unknown or not all(isinstance(change.get(k), str) for k in ("feature", "number", "to")):
                raise ValueError(f"Each change needs feature, number and to (and optionally was), as text: {change}")
            index, original = _outline.find_feature(snapshot, change["feature"])
            feature = touched.setdefault(original["featureId"], copy.deepcopy(original))
            numbers = _outline.editable(feature)
            hits = [(label, p) for label, p in numbers if label.lower() == change["number"].lower()]
            if "was" in change:
                hits = [(label, p) for label, p in hits if _same(p.get("expression"), change["was"], variables)]
            if len(hits) != 1:
                listing = "; ".join(f"{label} = {p.get('expression')}" for label, p in numbers) or "none"
                problem = "is not one of its numbers" if not hits else "matches more than one number; add \"was\" with the current value"
                raise LookupError(f"{index} \"{_outline.shown_name(original)}\": \"{change['number']}\" {problem}. Its numbers: {listing}")
            label, parameter = hits[0]
            old, new = parameter.get("expression"), change["to"]
            note = _check_value(old, new, variables)
            for other in feature.get("parameters", []):  # a variable stores its value twice; keep the two the same
                if other is not parameter and other.get("parameterId") == "value" and other.get("expression") == old:
                    other["expression"] = new
            parameter["expression"] = new
            lines.append(f"{index} \"{_outline.shown_name(original)}\": {label}  {old} -> {new}{note}")

        # Onshape rebuilds a sketch from its dimensions but keeps whatever coordinates we send, so solve every
        # affected sketch here and send the exact result. A value the sketch cannot take is caught now, for free.
        features = [touched.get(f.get("featureId"), f) for f in snapshot.data.get("features", [])]
        after = _outline.variables(Snapshot(snapshot.ref, snapshot.version, dict(snapshot.data, features=features)))
        moved_names = {name for name in after if name not in variables or after[name] != variables[name]}
        for index, feature in enumerate(features, start=1):
            if feature.get("featureType") != "newSketch" or feature.get("suppressed"):
                continue
            uses = any(f"#{name}" in str(p.get("expression", "")) for c in feature.get("constraints", [])
                       for p in c.get("parameters", []) for name in moved_names)
            if feature.get("featureId") not in touched and not uses:
                continue
            solved = copy.deepcopy(feature) if feature.get("featureId") not in touched else feature
            drawn, reasons = sketch_io.to_sketch(solved, after)
            if drawn is None:
                notes.append(f"{index} \"{feature.get('name')}\" could not be re-solved here (it uses {', '.join(reasons)}). Onshape "
                             "rebuilds it correctly, but cad_inspect may show its old coordinates. Trust cad_check's numbers.")
                continue
            gate = check(drawn, strict=False)
            if gate.unsolved:
                raise ValueError(f"With these values sketch {index} \"{feature.get('name')}\" cannot be solved: "
                                 "its dimensions contradict each other. Nothing was sent.")
            shift = sketch_io.write_back(solved, drawn)
            if shift > 1e-9:
                touched[feature["featureId"]] = solved
                notes.append(f"{index} \"{feature.get('name')}\" was re-solved here: its shapes move by up to {shift:.3f} mm.")
        return ChangePlan(lines, list(touched.values()), notes)

    def apply_changes(self, snapshot: Snapshot, plan: ChangePlan) -> WriteResult:
        body = {"btType": "BTUpdateFeaturesCall-1748", "features": plan.payload,
                "serializationVersion": snapshot.data.get("serializationVersion"),
                "libraryVersion": snapshot.data.get("libraryVersion")}
        answer = self._write(snapshot.ref, "/features/updates", body, snapshot.version)
        return WriteResult({fid: state.get("featureStatus", "UNKNOWN") for fid, state in (answer.get("featureStates") or {}).items()},
                           version=answer.get("sourceMicroversion"))

    # ---------------------------------------------------------------- adding features

    def add(self, ref: ModelRef, what: str, after: str | None = None, **spec) -> WriteResult:
        feature = getattr(build, what)(**spec)
        answer = self._write(ref, "/features", {"feature": feature}, after)
        fid = answer.get("feature", {}).get("featureId")
        return WriteResult({fid: answer.get("featureState", {}).get("featureStatus", "UNKNOWN")}, fid, answer.get("sourceMicroversion"))

    def name_parts(self, ref: ModelRef, names: dict[str, str]) -> dict[str, str]:
        made = self._evaluate(ref, BODIES_SCRIPT % json.dumps(list(names)))
        items, told = [], {}
        prefix = f"{self.client.base_url}/metadata/d/{ref.document}/{ref.workspace}/e/{ref.element}/p/"
        for fid, name in names.items():
            part_ids = [i[0] if isinstance(i, list) else i for i in made.get(fid, [])]
            if not part_ids:
                told[fid] = "this feature made no solid part"
                continue
            for n, part_id in enumerate(part_ids, start=1):
                items.append({"href": prefix + quote(str(part_id), safe=""),
                              "properties": [{"propertyId": build.NAME_PROPERTY, "value": name if len(part_ids) == 1 else f"{name} {n}"}]})
            told[fid] = f"named \"{name}\"" + (f" 1 to {len(part_ids)}" if len(part_ids) > 1 else "")
        if items:
            path = f"/metadata/d/{ref.document}/{ref.workspace}/e/{ref.element}"
            answer = self.client.post_json(path, {"items": items})
            failed = [i for i in (answer.get("items") or []) if i.get("status") not in (None, "SUCCEEDED")]
            if failed:
                raise OnshapeError(200, "Onshape did not accept the names: " + json.dumps(failed)[:500])
        return told

    # ---------------------------------------------------------------- repair

    def plan_repair(self, snapshot: Snapshot) -> ChangePlan:
        lines, payload = [], []
        for index, original in enumerate(snapshot.data.get("features", []), start=1):
            if original.get("featureType") != "newSketch":
                continue
            feature = copy.deepcopy(original)
            labelled = named = 0
            for n, constraint in enumerate(feature.get("constraints", []), start=1):
                parameters = constraint.get("parameters", [])
                ids = {p.get("parameterId") or "" for p in parameters}
                driven = any(p.get("parameterId") == "driven" and p.get("value") is True for p in parameters)
                if ids & {"length", "angle"} and not driven and not any(i.startswith("label") for i in ids):
                    if constraint.get("constraintType") == "ANGLE":
                        parameters += [build._quantity("labelAngle", "0.5*rad"), build._quantity("labelDistance", "0.010*m")]
                    else:
                        parameters += build.labels_for(constraint.get("constraintType", ""), n)
                    labelled += 1
                if not constraint.get("entityId"):
                    constraint["entityId"] = f"repaired{n}"
                    named += 1
            if labelled or named:
                what = [f"{labelled} dimension{'s get' if labelled > 1 else ' gets'} a label the sketch editor can show"] if labelled else []
                what += [f"{named} constraint{'s get' if named > 1 else ' gets'} an id so its symbol is drawn"] if named else []
                lines.append(f"{index} \"{feature.get('name')}\": " + "; ".join(what))
                payload.append(feature)
        return ChangePlan(lines, payload, ["Shapes, dimensions and values are not changed. One request per sketch."])

    def apply_repair(self, snapshot: Snapshot, plan: ChangePlan) -> WriteResult:
        statuses, version = {}, snapshot.version
        for feature in plan.payload:  # a whole-feature update, which is what lets parameters be added
            answer = self._write(snapshot.ref, "/features/featureid/" + quote(feature["featureId"], safe=""), {"feature": feature}, version)
            statuses[feature["featureId"]] = answer.get("featureState", {}).get("featureStatus", "UNKNOWN")
            version = answer.get("sourceMicroversion") or None
        return WriteResult(statuses, version=version)

    # ---------------------------------------------------------------- assemblies

    def read_assembly(self, ref: ModelRef) -> Snapshot:
        data = self.client.get_json(f"/assemblies/d/{ref.document}/{ref.workspace}/e/{ref.element}",
                                    {"includeMateFeatures": "true", "includeMateConnectors": "true", "includeNonSolids": "false"})
        return Snapshot(ref, data["rootAssembly"]["documentMicroversion"], data)

    def assembly(self, snapshot: Snapshot) -> Assembly:
        root = snapshot.data["rootAssembly"]
        if snapshot.data.get("subAssemblies") or any(i.get("type") != "Part" for i in root.get("instances", [])):
            raise ValueError("This assembly contains sub-assemblies, which this tool does not follow yet. "
                             "Check each sub-assembly on its own.")
        placed = {tuple(o["path"]): o for o in root.get("occurrences", [])}
        instances = {}
        for i in root.get("instances", []):
            if i.get("suppressed"):
                continue
            occurrence = placed.get((i["id"],), {})
            t = list(occurrence.get("transform") or [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
            t[3], t[7], t[11] = t[3] * 1000, t[7] * 1000, t[11] * 1000
            studio = ModelRef("onshape", i["documentId"], "m/" + i["documentMicroversion"], i["elementId"])
            instances[i["id"]] = Instance(i["id"], i.get("name", i["id"]), studio, i["partId"], t, bool(occurrence.get("fixed")))
        result = Assembly(instances)
        for feature in root.get("features", []):
            data = feature.get("featureData", {})
            if feature.get("suppressed"):
                continue
            if feature.get("featureType") == "mateGroup":
                members = [o["occurrence"][0] for o in data.get("occurrences", []) if o.get("occurrence") and o["occurrence"][0] in instances]
                result.groups.append((data.get("name", ""), members))
            elif feature.get("featureType") == "mate":
                sides = [e for e in data.get("matedEntities", []) if e.get("matedOccurrence") and e["matedOccurrence"][0] in instances]
                if len(sides) != 2:
                    continue
                frames = []
                for side in sides:
                    t, cs = instances[side["matedOccurrence"][0]].transform, side["matedCS"]
                    frames.append((transform_point(t, [c * 1000 for c in cs["origin"]]), transform_direction(t, cs["zAxis"])))
                (o1, z1), (o2, _) = frames
                gap = [b - a for a, b in zip(o1, o2)]
                along = sum(g * z for g, z in zip(gap, z1))
                off = sum((g - along * z) ** 2 for g, z in zip(gap, z1)) ** 0.5
                result.joints.append(Joint(data.get("name", ""), str(data.get("mateType", "")).lower(),
                                           (sides[0]["matedOccurrence"][0], sides[1]["matedOccurrence"][0]), o1, z1, off))
        return result

    def _evaluate(self, ref: ModelRef, script: str):
        """Run a read-only FeatureScript in the studio. A script that fails must never look like an empty answer."""
        answer = self.client.post_json(self._studio(ref) + "/featurescript", {"script": script, "queries": {}})
        if answer.get("result") is None:
            notices = "; ".join(str(n.get("message", n)) for n in answer.get("notices") or []) or "no reason given"
            raise OnshapeError(200, f"the FeatureScript evaluation returned nothing: {notices}"[:900])
        return _plain(answer["result"])

    def part_boxes(self, studio: ModelRef) -> list[dict]:
        out = []
        for ids, lo, hi, volume, name in self._evaluate(studio, PARTS_SCRIPT):
            out.append({"id": ids[0] if isinstance(ids, list) else ids, "name": name or "",
                        "lo": [v * 1000 for v in lo], "hi": [v * 1000 for v in hi], "volume": volume * 1e9})
        return out

    def explain(self, ref: ModelRef, feature_ids: list[str]) -> dict[str, str]:
        out = {}
        for fid, status in self._evaluate(ref, STATUS_SCRIPT % json.dumps(feature_ids)).items():
            status = status if isinstance(status, dict) else {}
            words = [str(status.get(k)) for k in ("statusEnum", "statusMsg") if status.get(k)]
            faulty = status.get("faultyParameters")
            out[fid] = "; ".join(words) + (f" (parameters: {', '.join(map(str, faulty))})" if faulty else "") or "no details given"
        return out


def _plain(value):
    """Onshape's typed FeatureScript values as plain Python."""
    if isinstance(value, dict):
        kind, inner = value.get("btType", ""), value.get("value")
        if kind.endswith("BTFSValueArray"):
            return [_plain(v) for v in inner]
        if kind.endswith("BTFSValueMap"):
            return {str(_plain(entry["key"])): _plain(entry["value"]) for entry in inner}
        return _plain(inner) if isinstance(inner, (dict, list)) else inner
    return value


def _same(expression: str | None, wanted: str, variables: dict[str, Quantity]) -> bool:
    if expression is None:
        return False
    if expression.replace(" ", "") == wanted.replace(" ", ""):
        return True
    try:
        return abs(evaluate(expression, variables).value - evaluate(wanted, variables).value) < 1e-9
    except ExpressionError:
        return False


def _check_value(old: str | None, new: str, variables: dict[str, Quantity]) -> str:
    """Refuse a new value that cannot be read or is the wrong kind of quantity. Returns a note for the plan line."""
    try:
        value = evaluate(new, variables)
    except UnknownVariable as e:
        return f"   (not checked here: {str(e).split(': ', 1)[-1].split(' (known')[0]})"
    try:
        before = evaluate(old or "", variables)
    except ExpressionError:
        return ""
    if value.kind != before.kind:
        hint = f', for example "{new} mm"' if before.kind == "length" and value.kind == "number" else ""
        raise ValueError(f"\"{new}\" is a {value.kind} but the current value \"{old}\" is a {before.kind}{hint}.")
    return ""
