"""The tools: a few operations at the level of design intent, defined once and offered to every AI client.

Each tool returns plain text made of numbers and names. None of them answers a bare "OK": a model that looks
fine still gets its numbers reported, so whoever reads the answer can judge it.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import GOAL, backends, runners, sketch
from .backends.base import CadBackend, ModelRef, Snapshot, WriteResult
from .backends.files import FileBackend
from .budget import Ledger
from .config import Config
from .expr import ExpressionError, evaluate, length
from .motion import Assembly, split
from .runners.base import Runner
from .store import Store

JOBS = Path(__file__).parent / "jobs"
LEDGER_KEYS = ("ledger", "year_limit", "reserve")
BASE_PLANES = ("Top", "Front", "Right")


@dataclass
class Answer:
    """A tool's reply when it has a picture as well as text."""

    text: str
    png: bytes | None = None


@dataclass
class Context:
    """Everything a tool needs, built once from the configuration."""

    config: Config
    ledger: Ledger
    backend: CadBackend  # the configured CAD system
    runner: Runner
    store: Store
    files: FileBackend = None  # mesh files on disk: always available, costs nothing

    def __post_init__(self):
        self.files = self.files or FileBackend()

    @classmethod
    def create(cls, config: Config) -> "Context":
        project, backend = config.section("project"), config.section("backend")
        store_dir = Path(config.section("store")["dir"])
        ledger = Ledger(
            Path(backend.get("ledger") or store_dir / "ledger.json"),
            project["name"], project["budget"], backend.get("year_limit"), backend.get("reserve", 20),
        )
        settings = {k: v for k, v in backend.items() if k not in LEDGER_KEYS}
        return cls(config, ledger, backends.create(settings, ledger), runners.create(config.section("runner")),
                   Store(store_dir))

    def resolve(self, model: str) -> tuple[CadBackend, ModelRef]:
        """Which backend a link or path belongs to, and the model it names."""
        backend = self.files if self.files.accepts(model) else self.backend
        return backend, backend.parse_ref(model)


def _spent(ctx: Context, before: int) -> str:
    return f"Requests: {ctx.ledger.session_used - before} this call. Budget: {ctx.ledger.line()}."


def _snapshot(ctx: Context, backend: CadBackend, ref: ModelRef, source: str) -> Snapshot:
    if source == "cached":
        snapshot = ctx.store.latest(ref)
        if snapshot is None:
            raise LookupError("There is no current copy of this model (it was never read, or it was changed since). "
                              "Read it with cad_read, source 'live' (1 request).")
        return snapshot
    if source != "live":
        raise ValueError("source must be 'live' or 'cached'")
    snapshot = backend.read_studio(ref)
    ctx.store.save(snapshot)
    return snapshot


def _tree_health(backend: CadBackend, snapshot: Snapshot) -> tuple[str, list[str]]:
    items = backend.outline(snapshot)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = f"{len(items)} features" + (": " + ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) if items else "")
    problems = [f"{i.index} \"{i.name}\" is {i.status}" for i in items if i.status != "OK" and not i.suppressed]
    return summary, problems


def _mesh(ctx: Context, backend: CadBackend, ref: ModelRef, version: str, source: str) -> Path:
    mesh = ctx.store.path(ref, version, "mesh.stl")
    if not mesh.exists():
        if source == "cached":
            raise LookupError("No mesh is saved for this version. Run cad_check with source 'live' first.")
        mesh.parent.mkdir(parents=True, exist_ok=True)
        mesh.write_bytes(backend.export_mesh(ref, version))
    return mesh


# ---------------------------------------------------------------- reading tools

def status(ctx: Context, probe_runner: bool = False) -> str:
    files = ", ".join(str(f) for f in ctx.config.files) or "none found (defaults: budget 0, local runner)"
    problem = ctx.backend.problem()
    lines = [
        f"Goal: {GOAL}",
        f"Config: {files}",
        f"CAD backends: {ctx.backend.name} (links{'; NOT READY: ' + problem if problem else ''}), "
        f"{ctx.files.name} (paths to .stl files, free)",
        f"Runner: {ctx.runner.describe()}",
        f"Store: {ctx.store.root}",
        f"Budget: {ctx.ledger.line()}",
    ]
    if probe_runner:
        started = time.monotonic()
        answer = ctx.runner.run(JOBS / "mesh_report.py", args=["--selftest"], timeout=60)
        volume = answer["bodies"][0]["volume"]
        lines.append(f"Runner check: a 10 mm test cube measured {volume:g} mm3 (expected 1000) "
                     f"in {time.monotonic() - started:.1f} s")
        modules = ", ".join(f"{name} {version}" if version else f"no {name}" for name, version in answer.get("modules", {}).items())
        lines.append(f"Runner has: Python {answer.get('python', '?')}, {modules}. cad_view needs PIL; cad_motion needs numpy and scipy.")
    return "\n".join(lines)


def read(ctx: Context, model: str, source: str = "live") -> str:
    before = ctx.ledger.session_used
    backend, ref = ctx.resolve(model)
    snapshot = _snapshot(ctx, backend, ref, source)
    summary, _ = _tree_health(backend, snapshot)
    lines = [f"Feature tree at version {snapshot.version[:8]} ({summary})" + (" [cached copy]" if source == "cached" else "")]
    rollback, total = snapshot.data.get("rollbackIndex"), len(snapshot.data.get("features", []))
    if isinstance(rollback, int) and 0 <= rollback < total:
        lines.append(f"The rollback bar is after feature {rollback}: the features below it are not built.")
    for item in backend.outline(snapshot):
        flags = ("" if item.status == "OK" else f"  ** {item.status} **") + ("  (suppressed)" if item.suppressed else "")
        lines.append(f"{item.index:>3}  {item.kind:<10} \"{item.name}\"{flags}" + ("  " + "; ".join(item.notes) if item.notes else ""))
        lines += [f"       {label} = {value}" for label, value in item.numbers]
    lines.append(_spent(ctx, before))
    return "\n".join(lines)


def inspect(ctx: Context, model: str, feature: str) -> str:
    backend, ref = ctx.resolve(model)
    snapshot = _snapshot(ctx, backend, ref, "cached")
    return f"From the cached copy at version {snapshot.version[:8]}:\n" + backend.describe_feature(snapshot, feature)


def check(ctx: Context, model: str, source: str = "live") -> str:
    before = ctx.ledger.session_used
    backend, ref = ctx.resolve(model)
    snapshot = _snapshot(ctx, backend, ref, source)
    report = _mesh_report(ctx, backend, ref, snapshot.version, source)
    summary, problems = _tree_health(backend, snapshot)
    bodies = report["bodies"]
    open_bodies = [n for n, body in enumerate(bodies, start=1) if not body["watertight"]]

    lines = [f"Geometry at version {snapshot.version[:8]}, measured on {ctx.runner.describe()}"]
    if problems:
        lines.append(f"REBUILD PROBLEMS ({len(problems)}): " + "; ".join(problems))
    if open_bodies:
        lines.append(f"NOT CLOSED: body {', '.join(map(str, open_bodies))} has gaps or flipped faces.")
    lines.append(f"Feature tree: {summary}.")
    if bodies:
        lo = [min(b["bbox_min"][k] for b in bodies) for k in range(3)]
        hi = [max(b["bbox_max"][k] for b in bodies) for k in range(3)]
        lines.append(f"{len(bodies)} bod{'y' if len(bodies) == 1 else 'ies'}, {_total(report):.2f} cm3 in total, "
                     f"overall {_size(lo, hi)} mm")
        for n, body in enumerate(bodies, start=1):
            lo, hi = body["bbox_min"], body["bbox_max"]
            span = ", ".join(f"{axis} {a:.2f}..{b:.2f}" for axis, a, b in zip("xyz", lo, hi))
            lines.append(f"{n:>3}  {body['volume'] / 1000:9.2f} cm3  {_size(lo, hi)} mm  at {span}"
                         + ("" if body["watertight"] else "  NOT CLOSED"))
            lines += _round_lines(body.get("round", []))
    else:
        lines.append("No solid bodies.")
    lines.append(_change(ctx, ref, snapshot.version, report))
    lines.append("Measured on the exported mesh: curved bodies read slightly small. Holes and bosses are full cylinders only.")
    lines.append(_spent(ctx, before))
    return "\n".join(lines)


def view(ctx: Context, model: str, source: str = "cached") -> Answer:
    before = ctx.ledger.session_used
    backend, ref = ctx.resolve(model)
    snapshot = _snapshot(ctx, backend, ref, source)
    picture = ctx.store.path(ref, snapshot.version, "view.png")
    if not picture.exists():
        answer = ctx.runner.run(JOBS / "render.py", [_mesh(ctx, backend, ref, snapshot.version, source)])
        picture.write_bytes(base64.b64decode(answer["png"]))
    text = (f"Four views of the model at version {snapshot.version[:8]}, saved as {picture}. "
            "A picture is for spotting a wrong shape; take every measurement from cad_check.\n" + _spent(ctx, before))
    return Answer(text, picture.read_bytes())


def _mesh_report(ctx: Context, backend: CadBackend, ref: ModelRef, version: str, source: str) -> dict:
    saved = ctx.store.path(ref, version, "mesh_report.json")
    if saved.exists():
        report = json.loads(saved.read_text())
        if all("round" in body for body in report["bodies"]):
            return report
    report = ctx.runner.run(JOBS / "mesh_report.py", [_mesh(ctx, backend, ref, version, source)])
    saved.write_text(json.dumps(report))
    return report


def _round_lines(features: list[dict]) -> list[str]:
    """Holes and bosses grouped by size: '5 x boss diameter 58.80 along Z, 25.42 long, centres (x, y): ...'."""
    groups: dict[tuple, list] = {}
    for f in features:
        groups.setdefault((f["kind"], round(f["diameter"], 2), tuple(f["axis"]), round(f["length"], 1)), []).append(f["center"])
    lines = []
    for (kind, diameter, axis, long), centres in groups.items():
        main = max(range(3), key=lambda k: abs(axis[k]))
        if abs(axis[main]) > 0.9999:  # along a model axis: give the two coordinates across it
            keep = [k for k in range(3) if k != main]
            where = f"centres ({'xyz'[keep[0]]}, {'xyz'[keep[1]]}): " + ", ".join(f"({_n(c[keep[0]])}, {_n(c[keep[1]])})" for c in centres[:10])
            along = "XYZ"[main]
        else:
            where = "centres: " + ", ".join(f"({_n(c[0])}, {_n(c[1])}, {_n(c[2])})" for c in centres[:10])
            along = "(" + ", ".join(f"{a:.3f}" for a in axis) + ")"
        more = f", and {len(centres) - 10} more" if len(centres) > 10 else ""
        lines.append(f"        {len(centres)} x {kind} diameter {diameter:.2f} along {along}, {long:.2f} long, {where}{more}")
    return lines


def _n(value: float) -> str:
    return f"{round(value, 2) + 0.0:.2f}"  # never "-0.00"


def _total(report: dict) -> float:
    return sum(b["volume"] for b in report["bodies"]) / 1000


def _size(lo: list[float], hi: list[float]) -> str:
    return " x ".join(f"{b - a:.2f}" for a, b in zip(lo, hi))


def _change(ctx: Context, ref: ModelRef, version: str, report: dict) -> str:
    history = ctx.store.versions(ref)
    earlier = history[:len(history) - 1 - history[::-1].index(version)] if version in history else history
    previous = next((v for v in reversed(earlier) if v != version and ctx.store.path(ref, v, "mesh_report.json").exists()), None)
    if previous is None:
        return "No earlier check of this model to compare with."
    old = json.loads(ctx.store.path(ref, previous, "mesh_report.json").read_text())
    return (f"Since version {previous[:8]}: bodies {len(old['bodies'])} -> {len(report['bodies'])}, "
            f"volume {_total(old):.2f} -> {_total(report):.2f} cm3 ({_total(report) - _total(old):+.2f}).")


# ---------------------------------------------------------------- motion

def _joint_line(assembly: Assembly, joint, number: int) -> str:
    a, b = (assembly.instances[i].name for i in joint.sides)
    where = ", ".join(_n(v) for v in joint.origin)
    way = ", ".join(f"{v:.3f}" for v in joint.axis)
    off = f"  ** its two sides are {joint.mismatch:.2f} mm apart: the joint is not solved **" if joint.mismatch > 0.01 else ""
    return f"{number}. {joint.kind} \"{joint.name}\": \"{a}\" to \"{b}\", axis through ({where}) mm along ({way}){off}"


def motion(ctx: Context, model: str, joint: str = "", from_deg: float = 0.0, to_deg: float = 90.0, step_deg: float = 5.0,
           source: str = "live") -> str:
    before = ctx.ledger.session_used
    backend, ref = ctx.resolve(model)
    if source == "live":
        snapshot = backend.read_assembly(ref)
        ctx.store.save(snapshot)
    else:
        snapshot = _snapshot(ctx, backend, ref, "cached")
    assembly = backend.assembly(snapshot)
    turning = [j for j in assembly.joints if j.kind != "fastened"]
    fixed = [i.name for i in assembly.instances.values() if i.fixed]
    lines = [f"Assembly at version {snapshot.version[:8]}: {len(assembly.instances)} parts, {len(assembly.groups)} rigid groups, "
             f"{len(turning)} joints that move. Fixed: {', '.join(fixed) or 'NOTHING (fix the base part)'}."]
    if len(fixed) > 1:
        lines.append(f"More than one part is fixed ({len(fixed)}). Fix only the base and hold the rest with joints.")
    if not joint:
        lines += [_joint_line(assembly, j, n) for n, j in enumerate(turning, start=1)]
        lines.append("Name a joint to sweep it: its angle range is measured from where the assembly stands now.")
        return "\n".join(lines + [_spent(ctx, before)])

    chosen = assembly.find_joint(joint)
    if chosen.kind != "revolute":
        raise ValueError(f"\"{chosen.name}\" is a {chosen.kind} joint. Only revolute joints can be swept so far.")
    moving, still, notes = split(assembly, chosen)
    steps = int(round((to_deg - from_deg) / step_deg)) if step_deg > 0 else -1
    if not 1 <= steps <= 120:
        raise ValueError("The sweep needs from_deg below to_deg and between 1 and 120 steps.")
    angles = [round(from_deg + n * step_deg, 6) for n in range(steps + 1)]

    studios: dict[ModelRef, dict] = {}
    for instance in assembly.instances.values():
        studios.setdefault(instance.studio, {"instances": []})["instances"].append(instance)
    meshes, scene = [], {"axis": {"origin": list(chosen.origin), "direction": list(chosen.axis)}, "angles": angles, "spacing": 4.0,
                         "joint_sides": [[assembly.instances[i].name for i in (moving if side in moving else
                                          next((m for _, m in assembly.groups if side in m), [side]))] for side in chosen.sides],
                         "studios": []}
    for studio, entry in studios.items():
        version = studio.workspace.split("/", 1)[1]
        meshes.append(_mesh(ctx, backend, studio, version, source))
        saved = ctx.store.path(studio, version, "parts.json")
        if not saved.exists():
            if source == "cached":
                raise LookupError("The parts of a studio are not saved for this version. Run cad_motion with source 'live' first.")
            listed = backend.part_boxes(studio)
            if not listed:
                raise RuntimeError("A Part Studio used by this assembly reports no solid parts, so the sweep cannot place its geometry.")
            saved.write_text(json.dumps(listed))
        parts = json.loads(saved.read_text())
        index = {p["id"]: n for n, p in enumerate(parts)}
        scene["studios"].append({"parts": [{"lo": p["lo"], "hi": p["hi"]} for p in parts], "instances": [
            {"name": i.name, "part": index.get(i.part, -2), "transform": i.transform, "moving": i.id in moving} for i in entry["instances"]]})
    scene_file = ctx.store.path(ref, snapshot.version, "scene.json")
    scene_file.write_text(json.dumps(scene))
    answer = ctx.runner.run(JOBS / "sweep.py", [scene_file, *meshes], timeout=1200)
    if answer.get("error"):
        raise RuntimeError(f"The sweep could not run: {answer['error']}. Parts without geometry: {answer.get('no_geometry')}")
    ctx.store.path(ref, snapshot.version, "sweep.json").write_text(json.dumps(answer))

    lines.append(_joint_line(assembly, chosen, turning.index(chosen) + 1))
    for side, found in zip(chosen.sides, answer["joint"]):
        label = f"the side of \"{assembly.instances[side].name}\"" + (f" ({len(found['parts'])} parts held together)" if len(found["parts"]) > 1 else "")
        if not found["parts"]:
            lines.append(f"   {label}: no geometry found, so the joint could not be checked against it.")
        elif found["round"]:
            shown = []
            for name, rings in list(found["round"].items())[:3]:
                shown.append(f"\"{name}\" (" + ", ".join(f"diameter {_n(d)}" + ("" if arc == 360 else f" over {arc} deg") for d, arc in rings[:3]) + ")")
            lines.append(f"   {label}: the axis is the centre of round geometry of " + "; ".join(shown) + ".")
        else:
            lines.append(f"   {label}: NO round geometry is centred on the axis. "
                         "If this side is meant to turn on a hole, bearing, pin or cradle, the joint is not sitting on it.")
    groups = [name for name, members in assembly.groups if members and set(members) <= set(moving)]
    lines.append(f"Moving: {len(moving)} parts" + (f" ({', '.join(groups)})" if groups else "") + f". Standing still: {len(still)} parts.")
    lines += notes
    if answer["no_geometry"]:
        lines.append(f"NOT MEASURED, no geometry matched: {', '.join(answer['no_geometry'][:8])}" + (" ..." if len(answer['no_geometry']) > 8 else ""))
    lines.append(f"Swept {_n(from_deg)} to {_n(to_deg)} deg in steps of {_n(step_deg)} from where it stands now ({len(angles)} positions), "
                 f"on {ctx.runner.describe()}. Surfaces sampled every {answer['spacing']:g} mm, the closest pairs every {answer['fine']:g} mm.")
    lines.append("Closest approaches, nearest first:")
    for pair in answer["pairs"][:14]:
        where = ", ".join(_n(v) for v in pair["where"])
        both = f"\"{pair['moving']}\" and \"{pair['still']}\""
        accuracy = "" if pair.get("refined") else f" (coarse: within {answer['spacing']:g} mm)"
        touch = answer.get("touch", 0.6)
        if pair["min"] < touch and len(pair["close_at"]) == len(angles):
            lines.append(f"  touching at every position: {both}  (a bearing surface, or a part resting on another)")
        elif pair["min"] < touch:
            span = f"{_n(min(pair['close_at']))} to {_n(max(pair['close_at']))} deg" if pair["close_at"] else f"{_n(pair['angle'])} deg"
            lines.append(f"  COLLIDES or touches (closer than {touch:g} mm) from {span}: {both}, near ({where})")
        else:
            lines.append(f"  {pair['min']:7.2f} mm at {_n(pair['angle'])} deg: {both}, near ({where}){accuracy}")
    if not answer["pairs"]:
        lines.append("  nothing comes within 60 mm.")
    lines.append(_spent(ctx, before))
    return "\n".join(lines)


# ---------------------------------------------------------------- writing tools

def _after_write(ctx: Context, backend: CadBackend, ref: ModelRef, result: WriteResult, names: dict[str, str]) -> list[str]:
    """Report what the CAD system said, in its own words when something failed (that costs one more request)."""
    ctx.store.mark_stale(ref)
    failed = result.failed()
    if not failed:
        return []
    reasons = backend.explain(ref, failed)
    return [f"  FAILED \"{names.get(fid, fid)}\": {result.statuses[fid]}: {reasons.get(fid, 'no details given')}" for fid in failed]


def _statuses(result: WriteResult) -> str:
    counts: dict[str, int] = {}
    for state in result.statuses.values():
        counts[state] = counts.get(state, 0) + 1
    return ", ".join(f"{n} {state}" for state, n in sorted(counts.items())) or "no statuses"


def set_numbers(ctx: Context, model: str, changes: list, dry_run: bool = True) -> str:
    before = ctx.ledger.session_used
    backend, ref = ctx.resolve(model)
    snapshot = _snapshot(ctx, backend, ref, "cached")
    if not changes:
        raise ValueError("No changes given.")
    plan = backend.plan_changes(snapshot, changes)
    lines = [f"Changes to the model as read at version {snapshot.version[:8]}:"] + ["  " + line for line in plan.lines]
    lines += ["  " + note for note in plan.notes]
    if dry_run:
        return "\n".join(lines + ["Dry run: nothing was sent. Sending these costs 1 request, all together. "
                                  "Call again with dry_run false to apply them."])
    result = backend.apply_changes(snapshot, plan)
    names = {fid: name for name, fid in backend.feature_names(snapshot).items()}
    problems = _after_write(ctx, backend, ref, result, names)
    lines.append(f"Sent. The changed features rebuild: {_statuses(result)}. Features that depend on them are not reported here.")
    lines += problems
    lines.append("The saved copy is now out of date. Run cad_check to see every feature's status and the new numbers.")
    lines.append(_spent(ctx, before))
    return "\n".join(lines)


def repair(ctx: Context, model: str, dry_run: bool = True) -> str:
    before = ctx.ledger.session_used
    backend, ref = ctx.resolve(model)
    snapshot = _snapshot(ctx, backend, ref, "cached")
    plan = backend.plan_repair(snapshot)
    if not plan.lines:
        return (f"Nothing to repair in the model as read at version {snapshot.version[:8]}: every dimension has a label "
                "and every constraint has an id.")
    lines = [f"Repairs to the model as read at version {snapshot.version[:8]}:"] + ["  " + line for line in plan.lines + plan.notes]
    if dry_run:
        return "\n".join(lines + [f"Dry run: nothing was sent. Applying this costs {len(plan.lines)} requests. "
                                  "Call again with dry_run false to apply it."])
    result = backend.apply_repair(snapshot, plan)
    names = {fid: name for name, fid in backend.feature_names(snapshot).items()}
    lines.append(f"Sent. The repaired sketches rebuild: {_statuses(result)}.")
    lines += _after_write(ctx, backend, ref, result, names)
    lines.append("The saved copy is now out of date. Run cad_check: the volumes must be exactly what they were.")
    lines.append(_spent(ctx, before))
    return "\n".join(lines)


def _points(value, size: int, what: str) -> list:
    if not (isinstance(value, list) and value and all(
            isinstance(p, list) and len(p) == size and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p) for p in value)):
        raise ValueError(f"{what} must be a list of [{', '.join('xyz'[:size])}] points in mm")
    return value


def build(ctx: Context, model: str, steps: list, dry_run: bool = True) -> str:
    before = ctx.ledger.session_used
    backend, ref = ctx.resolve(model)
    snapshot = _snapshot(ctx, backend, ref, "cached")
    variables = dict(backend.variables(snapshot))
    taken = dict(backend.feature_names(snapshot))  # every feature name in use -> id ("" for ones planned here)
    planes: dict[str, tuple] = {name: (None, name, 0.0) for name in BASE_PLANES}  # name -> (id or "new", base, offset mm)
    sketches: dict[str, sketch.Sketch] = {}
    makers: set[str] = set()  # features planned here that create a part
    planned, lines, problems, part_names = [], [], [], []

    def fresh(name, what: str) -> str:
        if not name or not isinstance(name, str):
            raise ValueError(f"{what} needs a name that says what it is")
        if name in taken:
            raise ValueError(f"there is already a feature called \"{name}\"")
        taken[name] = ""
        return name

    for number, step in enumerate(steps, start=1):
        kinds = ("variable", "plane", "sketch", "extrude", "fillet", "chamfer", "name_part")
        if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in kinds or not isinstance(next(iter(step.values())), dict):
            raise ValueError(f"Step {number} must be exactly one of " + ", ".join(f'{{"{k}": {{...}}}}' for k in kinds) + ".")
        kind, spec = next(iter(step.items()))
        try:
            if kind == "variable":
                name, value = spec.get("name"), str(spec.get("value", ""))
                if not isinstance(name, str) or not sketch.ID.match(name):
                    raise ValueError("a variable needs a name made of letters, digits and _")
                if name in variables:
                    raise ValueError(f"#{name} already exists; change it with cad_set")
                quantity = evaluate(value, variables)
                if quantity.kind == "number" and not spec.get("plain_number"):
                    raise ValueError(f"\"{value}\" has no unit. Write \"{value} mm\", or add \"plain_number\": true for a count")
                variables[name] = quantity
                planned.append((kind, f"#{name}", dict(name=name, expression=value, kind=quantity.kind)))
                lines.append(f"{number}. variable  #{name} = {value}")
            elif kind == "plane":
                name, base, offset = fresh(spec.get("name"), "a plane"), spec.get("from"), str(spec.get("offset", ""))
                if base not in BASE_PLANES:
                    raise ValueError("a plane is offset \"from\" Top, Front or Right")
                distance = length(offset, variables)
                if distance <= 0:
                    raise ValueError("offset must be greater than 0; use \"flip\": true for the other side")
                planes[name] = ("new", base, -distance if spec.get("flip") else distance)
                planned.append((kind, name, dict(name=name, base=base, offset=offset, flip=bool(spec.get("flip")))))
                lines.append(f"{number}. plane     \"{name}\": {offset} from {base}" + (", on the far side" if spec.get("flip") else ""))
            elif kind == "sketch":
                where = spec.get("plane", "Top")
                if where not in planes:
                    planes[where] = backend.plane_info(snapshot, str(where))
                drawn = sketch.from_spec(spec, variables, planes)
                fresh(drawn.name, "a sketch")
                gate = sketch.check(drawn)
                shapes = ", ".join(f"{e.kind} {e.id}" for e in drawn.entities.values())
                lines.append(f"{number}. sketch    \"{drawn.name}\" on {drawn.plane}: {shapes}")
                lines += ["     " + text for text in gate.lines()]
                if not gate.passed:
                    problems.append(f"step {number}: sketch \"{drawn.name}\" did not pass the gate")
                sketches[drawn.name] = drawn
                planned.append((kind, drawn.name, drawn))
            elif kind == "extrude":
                source = spec.get("sketch")
                if source in sketches:
                    target = ("new", source) + planes[sketches[source].plane][1:]
                else:
                    target = ("old",) + tuple(backend.sketch_plane(snapshot, str(source)))
                name = fresh(spec.get("name"), "an extrude")
                depth, operation, regions = str(spec.get("depth", "")), spec.get("operation", "new"), spec.get("regions", "all")
                if length(depth, variables) <= 0:
                    raise ValueError("depth must be greater than 0; use \"flip\": true to go the other way")
                if operation not in ("new", "add", "remove"):
                    raise ValueError("operation must be new, add or remove")
                if regions != "all":
                    _points(regions, 2, "regions (or \"all\")")
                if spec.get("part"):
                    if operation != "new":
                        raise ValueError("\"part\" names the part a new-operation extrude makes; this one only changes a part")
                    part_names.append((name, str(spec["part"])))
                if operation == "new":
                    makers.add(name)
                planned.append((kind, name, dict(name=name, target=target, regions=regions, depth=depth, operation=operation,
                                                 flip=bool(spec.get("flip")), symmetric=bool(spec.get("symmetric")))))
                where = "all regions (outline, inner shapes left as holes)" if regions == "all" else f"{len(regions)} picked region(s)"
                lines.append(f"{number}. extrude   \"{name}\": {where} of \"{source}\", {depth}, {operation}"
                             + (", flipped" if spec.get("flip") else "") + (", symmetric" if spec.get("symmetric") else "")
                             + (f", part named \"{spec['part']}\"" if spec.get("part") else ""))
            elif kind in ("fillet", "chamfer"):
                name = fresh(spec.get("name"), f"a {kind}")
                size_key = "radius" if kind == "fillet" else "width"
                size = str(spec.get(size_key, ""))
                if length(size, variables) <= 0:
                    raise ValueError(f"{size_key} must be greater than 0")
                edges = _points(spec.get("edges"), 3, "edges")
                planned.append((kind, name, {"name": name, "points": edges, size_key: size}))
                lines.append(f"{number}. {kind:<9} \"{name}\": {size_key} {size} on the {len(edges)} edge(s) nearest to the given points")
            else:
                made_by, name = str(spec.get("made_by", "")), spec.get("name")
                if not name or not isinstance(name, str):
                    raise ValueError("name_part needs the part's \"name\"")
                if made_by not in makers and not taken.get(made_by):
                    raise ValueError(f"\"made_by\" must be the feature that created the part; there is no feature \"{made_by}\"")
                part_names.append((made_by, name))
                lines.append(f"{number}. name      the part made by \"{made_by}\" becomes \"{name}\"")
        except (ValueError, LookupError, ExpressionError, AttributeError, RuntimeError) as e:
            problems.append(f"step {number} ({kind}): {e}")
            lines.append(f"{number}. {kind}: REFUSED: {e}")

    header = f"Plan for the model as read at version {snapshot.version[:8]}:"
    if problems:
        raise ValueError("\n".join(["Nothing was sent. Fix these first:"] + ["  " + p for p in problems] + ["", header] + lines))
    cost = len(planned) + (2 if part_names else 0)
    if dry_run:
        naming = " plus 2 to name the parts" if part_names else ""
        return "\n".join([header] + lines + [f"Dry run: nothing was sent. Building this costs {cost} requests: one per feature{naming}. "
                                              "Call again with dry_run false to build it."])

    done, ids, out = 0, {}, [header.replace("Plan for", "Built on")]
    names: dict[str, str] = {}
    version = snapshot.version
    for number, (kind, name, data) in enumerate(planned, start=1):
        if kind == "sketch":
            plane_id = planes[data.plane][0]
            result = backend.add(ref, "sketch", after=version, sk=data, plane_id=ids.get(data.plane) if plane_id == "new" else plane_id)
        elif kind == "extrude":
            data = dict(data)
            origin, source, base, offset = data.pop("target")
            result = backend.add(ref, "extrude", after=version, sketch_id=ids[source] if origin == "new" else source,
                                 plane=base, offset=offset, **data)
        else:
            result = backend.add(ref, kind, after=version, **data)
        ids[name], names[result.feature_id], version = result.feature_id, name, result.version
        failures = _after_write(ctx, backend, ref, result, names)
        if failures:
            out += [f"{number}. {kind} \"{name}\": added, but it does not rebuild:"] + failures
            out += [f"{n}. {k} \"{nm}\": not started" for n, (k, nm, _) in enumerate(planned, start=1) if n > number]
            out.append("Stopped at the first failure. The failed feature is in the document; fix or delete it there, "
                       "then read the model again.")
            break
        out.append(f"{number}. {kind} \"{name}\": added, {result.statuses.get(result.feature_id, 'UNKNOWN')}")
        done += 1
    if done == len(planned) and part_names:
        told = backend.name_parts(ref, {ids.get(made_by) or taken[made_by]: name for made_by, name in part_names})
        out += [f"   part of \"{made_by}\": {told.get(ids.get(made_by) or taken[made_by], 'not named')}" for made_by, _ in part_names]
    out.append(f"{done} of {len(planned)} features added. The saved copy is now out of date. "
               "Run cad_check to measure the result and compare it with what you meant to build.")
    out.append(_spent(ctx, before))
    return "\n".join(out)


# ---------------------------------------------------------------- registry

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    run: Callable[..., "str | Answer"]
    properties: dict
    required: tuple[str, ...] = ()

    def schema(self) -> dict:
        return {"type": "object", "properties": self.properties, "required": list(self.required),
                "additionalProperties": False}


MODEL = {"type": "string", "description": "The link to the CAD tab, exactly as copied from the browser, or the path of an .stl file."}
SOURCE = {"type": "string", "enum": ["live", "cached"], "default": "live",
          "description": "'live' asks the CAD system. 'cached' reuses the last copy read and costs 0 requests."}
DRY_RUN = {"type": "boolean", "default": True,
           "description": "true (the default) checks everything and sends nothing. false changes the document."}

TOOLS = [
    Tool("cad_status",
         "Show the project goal, the configuration in use, and the request budget. Costs 0 requests. "
         "Call this first in a session.",
         status,
         {"probe_runner": {"type": "boolean", "default": False,
                           "description": "Also run a tiny job on the runner, to prove that machine is reachable and say what it has."}}),
    Tool("cad_read",
         "Read a Part Studio's whole feature tree as a short outline: every feature in order, its status, the "
         "numbers a person would edit, and for each sketch whether it is fully defined and whether its dimensions are "
         "visible in the editor. Costs 1 request live, 0 cached. Read before changing anything: the CAD document is "
         "the only source of truth and the owner may have edited it by hand.",
         read, {"model": MODEL, "source": SOURCE}, ("model",)),
    Tool("cad_inspect",
         "Show everything about one feature from the copy cad_read saved: a sketch's entities, constraints and what "
         "can still move, or a feature's full parameter list. Costs 0 requests.",
         inspect,
         {"model": MODEL, "feature": {"type": "string", "description": "Position in the tree (1, 2, ...), feature id, or name."}},
         ("model", "feature")),
    Tool("cad_check",
         "Check the model by numbers: rebuild errors, then every solid body's volume, size and position, whether it "
         "is closed, its holes and bosses (diameter, axis, length, centres), and what changed since the last check. "
         "The geometry is pulled from the CAD document at one exact version and measured on the runner. Costs 3 "
         "requests when the model changed since the last check, 1 when it did not, 0 cached or for a file. Run it "
         "after every change and report its numbers, not just that it passed.",
         check, {"model": MODEL, "source": SOURCE}, ("model",)),
    Tool("cad_view",
         "A picture of the model from four sides, drawn on the runner from the geometry cad_check saved. Costs 0 "
         "requests after a check. Use it to spot a wrong shape; never measure from it.",
         view, {"model": MODEL, "source": dict(SOURCE, default="cached")}, ("model",)),
    Tool("cad_motion",
         "Check an assembly's motion. Without a joint: list the joints, their axes and which part is fixed (1 request). "
         "With a joint: say whether its axis sits on real round geometry of the two parts it joins, turn the moving side "
         "through the range (angles are measured from where the assembly stands now), and report how close every "
         "moving part comes to every still one, with the angle and place. Costs 1 request plus 3 per Part Studio whose "
         "geometry is not yet saved for this version; 0 cached. Needs numpy and scipy on the runner.",
         motion,
         {"model": dict(MODEL, description="The link to the assembly tab, exactly as copied from the browser."),
          "joint": {"type": "string", "default": "", "description": "Joint name or number. Leave empty to list the joints."},
          "from_deg": {"type": "number", "default": 0}, "to_deg": {"type": "number", "default": 90},
          "step_deg": {"type": "number", "default": 5}, "source": SOURCE},
         ("model",)),
    Tool("cad_set",
         "Change numbers that already exist in the model: a dimension, a depth, a variable. All changes go in one "
         "request, and the CAD system refuses them if the owner edited the document after the last cad_read. "
         "Each change is {\"feature\": position, name or id; \"number\": its label exactly as cad_read shows it, "
         "for example \"depth\" or \"diameter(circle 3)\" or \"#thickness\"; \"to\": the new value with its unit; "
         "\"was\": optionally the current value, needed when two numbers share a label}. Affected sketches are "
         "solved here first, so a value a sketch cannot take is refused before anything is sent. "
         "A dry run (the default) costs 0 requests and shows old and new values; sending costs 1.",
         set_numbers,
         {"model": MODEL,
          "changes": {"type": "array", "minItems": 1, "items": {
              "type": "object", "required": ["feature", "number", "to"], "additionalProperties": False,
              "properties": {"feature": {"type": "string"}, "number": {"type": "string"},
                             "to": {"type": "string"}, "was": {"type": "string"}}}},
          "dry_run": DRY_RUN},
         ("model", "changes")),
    Tool("cad_build",
         "Add features at the end of the tree. Every sketch must pass the gate (fully defined, nothing repeated, "
         "drawn where its dimensions say) or nothing at all is sent. Steps, in order, each exactly one of: "
         "{\"variable\": {\"name\": \"thickness\", \"value\": \"20 mm\"}}; "
         "{\"plane\": {\"name\": ..., \"from\": \"Top\"|\"Front\"|\"Right\", \"offset\": \"265 mm\", \"flip\": false}}; "
         "{\"sketch\": {\"name\": ..., \"plane\": \"Top\"|\"Front\"|\"Right\" or a plane's name, \"entities\": [...], \"constraints\": [...]}}; "
         "{\"extrude\": {\"name\": ..., \"sketch\": name, \"depth\": \"5 mm\", \"operation\": \"new\"|\"add\"|\"remove\", "
         "\"regions\": \"all\" or [[x, y], ...] in sketch coordinates, \"flip\": false, \"symmetric\": false, \"part\": name for the new part}}; "
         "{\"fillet\": {\"name\": ..., \"radius\": \"5 mm\", \"edges\": [[x, y, z], ...]}} and "
         "{\"chamfer\": {\"name\": ..., \"width\": \"1 mm\", \"edges\": [...]}}, each edge given by a point on it, in model mm; "
         "{\"name_part\": {\"made_by\": feature name, \"name\": ...}}. "
         "Sketch entities, in mm: {\"id\": \"hole\", \"circle\": [cx, cy, r]}, {\"line\": [x0, y0, x1, y1]}, "
         "{\"arc\": [cx, cy, r, from_deg, to_deg]} (counter-clockwise), {\"point\": [x, y]}, "
         "{\"rectangle\": [x0, y0, x1, y1]} (four lines id.bottom, id.right, id.top, id.left, already closed and squared); "
         "add \"construction\": true for reference geometry. "
         "Points: line.start, line.end, circle.center, arc.center, arc.start, arc.end, origin. "
         "Constraints: [\"coincident\", a, b], [\"horizontal\"|\"vertical\", line] or [.., point, point], "
         "[\"parallel\"|\"perpendicular\"|\"equal\", a, b], [\"midpoint\", point, point, point], "
         "[\"diameter\", circle, value], [\"radius\", arc, value], [\"length\", line, value], "
         "[\"distance\"|\"distance_x\"|\"distance_y\", point, point, value]. Values carry units and may use #variables. "
         "Dimension from the origin or from real shapes, with the numbers the owner would measure. Not available: "
         "tangent, angle, mirror, pattern; round corners with a fillet step instead of sketch arcs. "
         "A dry run (the default) costs 0 requests and shows the plan and the gate result; building costs 1 request "
         "per feature, plus 2 if parts are named. Read guide://editable-models first.",
         build,
         {"model": MODEL, "steps": {"type": "array", "minItems": 1, "items": {"type": "object"}}, "dry_run": DRY_RUN},
         ("model", "steps")),
    Tool("cad_repair",
         "Fix what makes an existing model hard to edit by hand without changing its shape: dimensions that have no "
         "label, which the sketch editor may not show, and constraints with no id, whose symbols are not drawn. "
         "cad_read says which sketches need it. A dry run (the default) costs 0 requests and lists the repairs; "
         "applying costs 1 request per sketch.",
         repair, {"model": MODEL, "dry_run": DRY_RUN}, ("model",)),
]
