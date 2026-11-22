#!/usr/bin/env python3
"""Live smoke test: the whole loop against real Onshape, with every request counted.

    python3 tests/live_smoke.py URL                  URL of an EMPTY Part Studio you can write to
    python3 tests/live_smoke.py --new-document       make a scratch document first (2 more requests; public on free plans)
    python3 tests/live_smoke.py --extras URL         only the second stage, on a studio the first stage has been run on

First stage (12 requests): build a small plate with a hole, measure it, change two numbers, measure again, and
check that a change based on an out-of-date read is refused. Second stage (15 requests): an offset plane, a post
from a picked region, a fillet, a chamfer, a part name, and the repair of a sketch made without dimension labels.
It stops at the first surprise and never retries.
Unit tests cannot tell when Onshape changes what it accepts; this can. Run it before a release.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from onshape_mcp import config, sketch, tools  # noqa: E402
from onshape_mcp.backends.onshape import build as onshape_build  # noqa: E402
from onshape_mcp.backends.base import Snapshot  # noqa: E402
from onshape_mcp.jobs import mesh_report  # noqa: E402

STEPS = [
    {"variable": {"name": "smoke_t", "value": "5 mm"}},
    {"sketch": {"name": "Smoke plate outline", "plane": "Top", "entities": [
        {"id": "plate", "rectangle": [0, 0, 60, 40]},
        {"id": "hole", "circle": [15, 20, 3]},
    ], "constraints": [
        ["coincident", "plate.bottom.start", "origin"],
        ["length", "plate.bottom", "60 mm"],
        ["length", "plate.right", "40 mm"],
        ["diameter", "hole", "6 mm"],
        ["distance_x", "origin", "hole.center", "15 mm"],
        ["distance_y", "origin", "hole.center", "#smoke_t * 4"],
    ]}},
    {"extrude": {"name": "Smoke plate", "sketch": "Smoke plate outline", "depth": "#smoke_t"}},
]
CHANGES = [
    {"feature": "Smoke plate outline", "number": "diameter(circle 1)", "to": "10 mm", "was": "6 mm"},
    {"feature": "#smoke_t", "number": "#smoke_t", "to": "6 mm"},
]


EXTRAS = [
    {"plane": {"name": "Smoke post base", "from": "Top", "offset": "#smoke_t"}},
    {"sketch": {"name": "Smoke post outline", "plane": "Smoke post base", "entities": [{"id": "post", "circle": [45, 20, 6]}],
                "constraints": [["diameter", "post", "12 mm"], ["distance_x", "origin", "post.center", "45 mm"],
                                ["distance_y", "origin", "post.center", "20 mm"]]}},
    {"extrude": {"name": "Smoke post", "sketch": "Smoke post outline", "regions": [[45, 20]], "depth": "10 mm", "operation": "add"}},
    {"fillet": {"name": "Smoke plate corners", "radius": "4 mm", "edges": [[0, 0, 3], [60, 0, 3], [60, 40, 3], [0, 40, 3]]}},
    {"chamfer": {"name": "Smoke post top", "width": "1 mm", "edges": [[51, 20, 16]]}},
    {"name_part": {"made_by": "Smoke plate", "name": "Smoke bracket"}},
]
OLD_STYLE = {"name": "Smoke old-style sketch", "plane": "Front", "entities": [{"id": "ring", "circle": [30, 3, 2]}],
             "constraints": [["diameter", "ring", "4 mm"], ["distance_x", "origin", "ring.center", "30 mm"],
                             ["distance_y", "origin", "ring.center", "3 mm"]]}


def extras(ctx, url: str) -> None:
    """Second stage, on the studio the first stage left: planes, picked regions, fillet, chamfer, part names, repair."""
    backend, ref = ctx.resolve(url)
    start = ctx.ledger.session_used

    say("7. A sketch the old way, without dimension labels (sent raw, to have something to repair)")
    drawn = sketch.from_spec(OLD_STYLE, {})
    expect(sketch.check(drawn).passed, "the old-style sketch is fully defined")
    feature = onshape_build.sketch(drawn)
    for constraint in feature["constraints"]:
        constraint["parameters"] = [p for p in constraint["parameters"] if not p["parameterId"].startswith("label")]
    backend.client.post_json(backend._studio(ref, writing=True) + "/features", {"feature": feature})

    text = tools.read(ctx, url)
    say("8. Read", text)
    expect("3 dimensions have no label" in text, "cad_read flags the three hidden dimensions")
    expect(text.count("fully defined") >= 2, "both sketches are reported fully defined")

    say("9. Build: plane, sketch on it, post from a picked region, fillet, chamfer, part name",
        tools.build(ctx, url, EXTRAS, dry_run=False))

    say("10. Check", tools.check(ctx, url))
    built = ctx.store.latest(ref)
    expect(all(i.status == "OK" for i in backend.outline(built)), "every feature rebuilds")
    got = body(ctx, ref)
    want = (60 * 40 - math.pi * 5 ** 2 - (4 - math.pi) * 4 ** 2) * 6 + math.pi * 6 ** 2 * 10 - 2 * math.pi * (6 - 1 / 3) * 0.5
    expect(abs(got["volume"] - want) / want < 0.005, f"volume {got['volume']:.1f} mm3 is within 0.5 % of {want:.1f}")
    expect(got["bbox_max"] == [60, 40, 16], "the post stands 10 mm above the 6 mm plate")
    found = {(f["kind"], f["diameter"]): f for f in got["round"]}
    expect(near(found[("hole", 10.0)]["center"][:2], (15, 24)), "the hole is still 10 mm at (15, 24)")
    expect(near(found[("boss", 12.0)]["center"][:2], (45, 20)) and abs(found[("boss", 12.0)]["length"] - 9) < 0.01,
           "the post is 12 mm at (45, 20), 9 mm of straight wall under a 1 mm chamfer")

    say("11. Repair the hidden dimensions", tools.repair(ctx, url, dry_run=False))
    text = tools.read(ctx, url)
    say("12. Read again", text)
    expect("no label" not in text, "no sketch has hidden dimensions any more")
    expect("ERROR" not in text and "WARNING" not in text, "every feature still rebuilds")

    say("13. Read the part names back")
    names = [p["name"] for p in backend.part_boxes(ref)]
    expect(names == ["Smoke bracket"], f"the part is called {names}")
    say(f"Done: {ctx.ledger.session_used - start} requests for the second stage. {ctx.ledger.line()}")


def say(title: str, text: str = "") -> None:
    print(f"\n=== {title}" + ("\n" + text if text else ""), flush=True)


def expect(condition: bool, what: str) -> None:
    print(("  ok    " if condition else "  WRONG ") + what, flush=True)
    if not condition:
        sys.exit("Stopped: the result is not what was expected. Nothing is retried.")


def hole(ctx, ref) -> tuple[float, float, float]:
    """Centre and diameter of the hole, measured on the bottom face of the exported mesh."""
    version = ctx.store.latest(ref).version
    rim = {p for triangle in mesh_report.read_triangles(ctx.store.path(ref, version, "mesh.stl").read_bytes()) for p in triangle
           if abs(p[2]) < 1e-6 and 0.01 < p[0] < 59.99 and 0.01 < p[1] < 39.99}
    cx, cy = sum(p[0] for p in rim) / len(rim), sum(p[1] for p in rim) / len(rim)
    return cx, cy, 2 * sum(math.hypot(p[0] - cx, p[1] - cy) for p in rim) / len(rim)


def near(got: tuple, want: tuple) -> bool:
    return all(abs(g - w) < 0.01 for g, w in zip(got, want))


def body(ctx, ref) -> dict:
    snapshot = ctx.store.latest(ref)
    report = json.loads(ctx.store.path(ref, snapshot.version, "mesh_report.json").read_text())
    expect(len(report["bodies"]) == 1, f"one solid body (found {len(report['bodies'])})")
    return report["bodies"][0]


def main(argv: list[str]) -> None:
    ctx = tools.Context.create(config.load())
    if len(argv) == 3 and argv[1] == "--extras":
        return extras(ctx, argv[2])
    if argv[1:] == ["--new-document"]:
        client = ctx.backend.client
        made = client.post_json("/documents", {"name": "onshape-mcp live test", "isPublic": True})
        did, wid = made["id"], made["defaultWorkspace"]["id"]
        studio = next(e for e in client.get_json(f"/documents/d/{did}/w/{wid}/elements") if e["elementType"] == "PARTSTUDIO")
        url = f"https://cad.onshape.com/documents/{did}/w/{wid}/e/{studio['id']}"
        say("New scratch document", url)
    elif len(argv) == 2:
        url = argv[1]
    else:
        sys.exit(__doc__)
    ref = ctx.backend.parse_ref(url)
    start = ctx.ledger.session_used

    say("1. Read the empty studio", tools.read(ctx, url))
    expect(not ctx.store.latest(ref).data.get("features"), "the studio is empty")

    say("2. Build: variable, sketch, extrude", tools.build(ctx, url, STEPS, dry_run=False))

    say("3. Check", tools.check(ctx, url))
    built = ctx.store.latest(ref)
    expect(all(i.status == "OK" for i in ctx.backend.outline(built)), "every feature rebuilds")
    drawn = next(f for f in built.data["features"] if f.get("featureType") == "newSketch")
    stored = next(e for e in drawn["entities"] if e["entityId"] == "hole")["geometry"]
    expect(abs(stored["xCenter"] - 0.015) < 1e-9 and abs(stored["yCenter"] - 0.020) < 1e-9 and abs(stored["radius"] - 0.003) < 1e-9,
           "Onshape stored the sketch exactly where the gate solved it")
    outline = next(i for i in ctx.backend.outline(built) if i.kind == "sketch")
    expect(not any("no label" in note for note in outline.notes), "every dimension kept its label (visible in the editor)")
    want = (60 * 40 - math.pi * 3 ** 2) * 5
    got = body(ctx, ref)
    expect(abs(got["volume"] - want) / want < 0.005, f"volume {got['volume']:.1f} mm3 is within 0.5 % of {want:.1f}")
    expect(got["bbox_max"] == [60, 40, 5] and got["bbox_min"] == [0, 0, 0] and got["watertight"], "size 60 x 40 x 5 mm, closed")
    expect(near(hole(ctx, ref), (15, 20, 6)), "the hole is 6 mm at (15, 20), measured on the mesh")

    say("4. Change two numbers in one request", tools.set_numbers(ctx, url, CHANGES, dry_run=False))

    say("5. Check again", tools.check(ctx, url))
    expect(near(hole(ctx, ref), (15, 24, 10)), "the hole is now 10 mm and moved with the variable to (15, 24), measured on the mesh")
    want = (60 * 40 - math.pi * 5 ** 2) * 6
    got = body(ctx, ref)
    expect(abs(got["volume"] - want) / want < 0.005, f"volume {got['volume']:.1f} mm3 is within 0.5 % of {want:.1f}")
    expect(got["bbox_max"] == [60, 40, 6], "thickness 6 mm")

    say("6. A change based on the earlier read must be refused")
    stale = Snapshot(ref, built.version, built.data)
    plan = ctx.backend.plan_changes(stale, [{"feature": "Smoke plate", "number": "depth", "to": "1 mm"}])
    try:
        ctx.backend.apply_changes(stale, plan)
        expect(False, "Onshape refused the out-of-date change")
    except RuntimeError as e:
        expect("changed after it was read" in str(e), f"refused: {e}")

    say(f"Done: {ctx.ledger.session_used - start} requests for the loop, {ctx.ledger.session_used} in this run. {ctx.ledger.line()}")
    extras(ctx, url)
    print(url)


if __name__ == "__main__":
    main(sys.argv)
