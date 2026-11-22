"""The tools end to end against a stand-in Onshape, and the MCP server speaking the protocol."""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import meshes
from fake_onshape import TAB, context

from onshape_mcp import GOAL, tools
from onshape_mcp.cli import main
from onshape_mcp.mcp_server import GUIDES, INSTRUCTIONS, guides, serve

ROOT = Path(__file__).parent.parent
SRC = str(ROOT / "src")
PLATE = {"sketch": {"name": "Bracket outline", "plane": "Top", "entities": [
    {"id": "plate", "rectangle": [0, 0, 60, 40]},
    {"id": "hole", "circle": [15, 20, 3]},
], "constraints": [
    ["coincident", "plate.bottom.start", "origin"],
    ["length", "plate.bottom", "60 mm"],
    ["length", "plate.right", "40 mm"],
    ["diameter", "hole", "6 mm"],
    ["distance_x", "origin", "hole.center", "15 mm"],
    ["distance_y", "origin", "hole.center", "#thickness"],
]}}
EXTRUDE = {"extrude": {"name": "Bracket", "sketch": "Bracket outline", "depth": "#thickness / 4"}}


class ReadAndCheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ctx, self.cad = context(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_read_gives_the_outline_and_the_cached_copy_is_free(self):
        text = tools.read(self.ctx, TAB)
        self.assertIn("Feature tree at version 11111111 (4 features: 1 ERROR, 3 OK)", text)
        self.assertIn('  2  sketch     "Layout"  on the Top plane', text)
        self.assertIn("       diameter(circle 1) = 180 mm", text)
        self.assertIn('  4  transform  "Tilt the drive"  ** ERROR **', text)
        self.assertIn("Requests: 1 this call. Budget: project 'demo' 1/20.", text)
        again = tools.read(self.ctx, TAB, source="cached")
        self.assertIn("[cached copy]", again)
        self.assertIn("Requests: 0 this call.", again)
        self.assertIn("circle 2", tools.inspect(self.ctx, TAB, "Layout"))

    def test_check_reports_problems_numbers_and_what_changed(self):
        first = tools.check(self.ctx, TAB)
        self.assertIn('REBUILD PROBLEMS (1): 4 "Tilt the drive" is ERROR', first)
        self.assertIn("1 body, 1.00 cm3 in total, overall 10.00 x 10.00 x 10.00 mm", first)
        self.assertIn("No earlier check of this model", first)
        self.assertIn("Requests: 3 this call.", first)  # the read, the export, and the export's redirect
        self.assertIn("Requests: 1 this call.", tools.check(self.ctx, TAB))  # unchanged: only the read
        self.assertIn("Requests: 0 this call.", tools.check(self.ctx, TAB, source="cached"))
        self.cad.edit_by_owner()
        tools.read(self.ctx, TAB)  # a read with no measurement in between must not hide the last measured version
        self.cad.edit_by_owner()
        self.cad.cube = 20
        self.assertIn("Since version 11111111: bodies 1 -> 1, volume 1.00 -> 8.00 cm3 (+7.00).", tools.check(self.ctx, TAB))

    def test_nothing_cached_and_no_budget_are_explained(self):
        with self.assertRaisesRegex(LookupError, "Read it with cad_read"):
            tools.read(self.ctx, TAB, source="cached")
        broke, _ = context(self.dir.name + "/b", budget=0)
        with self.assertRaisesRegex(RuntimeError, "Nothing was sent"):
            tools.read(broke, TAB)

    def test_status_states_the_goal_and_can_probe_the_runner(self):
        text = tools.status(self.ctx, probe_runner=True)
        self.assertIn(GOAL, text)
        self.assertIn("measured 1000 mm3 (expected 1000)", text)


class SetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ctx, self.cad = context(self.dir.name)
        tools.read(self.ctx, TAB)

    def tearDown(self):
        self.dir.cleanup()

    def test_a_dry_run_shows_old_and_new_and_sends_nothing(self):
        text = tools.set_numbers(self.ctx, TAB, [{"feature": "Plate", "number": "depth", "to": "#thickness / 4"},
                                                 {"feature": "2", "number": "diameter(circle 2)", "to": "60 mm", "was": "58.8 mm"}])
        self.assertIn('3 "Plate": depth  1 mm -> #thickness / 4', text)
        self.assertIn('2 "Layout": diameter(circle 2)  58.8 mm -> 60 mm', text)
        self.assertIn("Dry run: nothing was sent", text)
        self.assertEqual(len(self.cad.sent), 1)  # only the read

    def test_all_changes_go_in_one_request_pinned_to_the_version_read(self):
        text = tools.set_numbers(self.ctx, TAB, [{"feature": "Plate", "number": "depth", "to": "2 mm"},
                                                 {"feature": "#thickness", "number": "#thickness", "to": "18 mm"}], dry_run=False)
        method, path, body = self.cad.sent[-2]  # the last request asks why feature 4 is in error
        self.assertTrue(path.endswith("/features/updates"))
        self.assertIn('1 "#thickness": #thickness  20 mm -> 18 mm', text)
        self.assertEqual((body["sourceMicroversion"], body["rejectMicroversionSkew"]), ("1" * 24, True))
        self.assertEqual(len(body["features"]), 2)
        variable = next(f for f in self.cad.features if f["featureType"] == "assignVariable")
        self.assertEqual({p["expression"] for p in variable["parameters"] if p["parameterId"] in ("lengthValue", "value")}, {"18 mm"})
        self.assertIn("Requests: 2 this call.", text)  # the change, and asking why feature 4 is in error
        self.assertIn('FAILED "Tilt the drive": ERROR: EXTRUDE_NO_SELECTED_REGION', text)
        with self.assertRaisesRegex(LookupError, "changed since"):  # the saved copy must not be trusted any more
            tools.read(self.ctx, TAB, source="cached")

    def test_a_change_is_refused_when_the_owner_edited_after_the_read(self):
        self.cad.edit_by_owner()
        with self.assertRaisesRegex(RuntimeError, "changed after it was read, so nothing was changed"):
            tools.set_numbers(self.ctx, TAB, [{"feature": "Plate", "number": "depth", "to": "2 mm"}], dry_run=False)
        self.assertEqual(next(p["expression"] for f in self.cad.features if f["name"] == "Plate"
                              for p in f["parameters"] if p["parameterId"] == "depth"), "1 mm")

    def test_wrong_labels_units_and_kinds_are_refused_before_sending(self):
        for change, why in (({"feature": "Plate", "number": "height", "to": "2 mm"}, "is not one of its numbers"),
                            ({"feature": "Plate", "number": "depth", "to": "2"}, 'for example "2 mm"'),
                            ({"feature": "Plate", "number": "depth", "to": "30 deg"}, "is a angle but the current value"),
                            ({"feature": "Plate", "number": "depth", "to": "2 mm", "was": "9 mm"}, "is not one of its numbers"),
                            ({"feature": "Nothing", "number": "depth", "to": "2 mm"}, "No feature")):
            with self.assertRaisesRegex((ValueError, LookupError), why, msg=change):
                tools.set_numbers(self.ctx, TAB, [change], dry_run=False)
        self.assertEqual(len(self.cad.sent), 1)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ctx, self.cad = context(self.dir.name)
        tools.read(self.ctx, TAB)

    def tearDown(self):
        self.dir.cleanup()

    def test_dry_run_shows_the_plan_the_gate_and_the_cost(self):
        text = tools.build(self.ctx, TAB, [PLATE, EXTRUDE])
        self.assertIn('1. sketch    "Bracket outline" on Top', text)
        self.assertIn("Fully defined: 0 free movements, 0 repeated constraints.", text)
        self.assertIn('2. extrude   "Bracket": all regions', text)
        self.assertIn("Building this costs 2 requests", text)
        self.assertEqual(len(self.cad.sent), 1)

    def test_builds_one_feature_per_request_with_visible_dimensions(self):
        text = tools.build(self.ctx, TAB, [{"variable": {"name": "gap", "value": "0.2 mm"}}, PLATE, EXTRUDE], dry_run=False)
        self.assertIn("3 of 3 features added", text)
        self.assertIn("Requests: 3 this call.", text)
        variable, drawn, extrude = (body["feature"] for _, _, body in self.cad.sent[-3:])
        self.assertEqual(variable["featureType"], "assignVariable")
        ids = [c["entityId"] for c in drawn["constraints"]]
        self.assertEqual(len(ids), len(set(ids)))  # every constraint has its own id, or its symbol is not drawn
        for constraint in drawn["constraints"]:
            parameters = {p["parameterId"] for p in constraint["parameters"]}
            if "length" in parameters:  # every dimension has a label, or the editor shows no number
                self.assertTrue(parameters & {"labelDistance", "labelAngle"}, constraint)
        self.assertEqual(drawn["entities"][0]["entityId"], "origin")
        circle = next(e for e in drawn["entities"] if e["entityId"] == "hole")
        self.assertEqual((circle["btType"], circle["geometry"]["radius"], circle["geometry"]["yCenter"]), ("BTMSketchCurve-4", 0.003, 0.02))
        self.assertEqual(extrude["parameters"][2]["queries"][0]["featureId"], drawn["featureId"] if "featureId" in drawn else self.cad.features[-2]["featureId"])
        self.assertEqual(next(p["expression"] for p in extrude["parameters"] if p["parameterId"] == "depth"), "#thickness / 4")

    def test_an_underdefined_sketch_stops_everything_and_says_what_can_move(self):
        loose = json.loads(json.dumps(PLATE))
        loose["sketch"]["constraints"] = loose["sketch"]["constraints"][:4]  # the hole is not located
        with self.assertRaises(ValueError) as raised:
            tools.build(self.ctx, TAB, [loose, EXTRUDE], dry_run=False)
        text = str(raised.exception)
        self.assertIn("Nothing was sent", text)
        self.assertIn("NOT fully defined: 2 movements still free", text)
        self.assertIn("circle 'hole': centre x, centre y", text)
        self.assertEqual(len(self.cad.sent), 1)

    def test_bad_steps_are_all_reported_at_once(self):
        with self.assertRaises(ValueError) as raised:
            tools.build(self.ctx, TAB, [{"variable": {"name": "thickness", "value": "3 mm"}},
                                        {"variable": {"name": "count", "value": "4"}},
                                        {"extrude": {"name": "Lid", "sketch": "No such sketch", "depth": "2 mm"}},
                                        {"extrude": {"name": "Lid", "sketch": "Layout", "depth": "2"}}])
        text = str(raised.exception)
        for why in ("#thickness already exists", "has no unit", "No feature 'No such sketch'", 'needs a length unit'):
            self.assertIn(why, text)

    def test_a_feature_that_does_not_rebuild_stops_the_batch_and_explains(self):
        bad = {"extrude": {"name": "Bad pocket", "sketch": "Layout", "depth": "2 mm", "operation": "remove", "regions": [[0, 59]]}}
        text = tools.build(self.ctx, TAB, [bad, {"extrude": {"name": "Rim", "sketch": "Layout", "depth": "2 mm"}}], dry_run=False)
        self.assertIn('1. extrude "Bad pocket": added, but it does not rebuild:', text)
        self.assertIn("ERROR: EXTRUDE_NO_SELECTED_REGION", text)
        self.assertIn('2. extrude "Rim": not started', text)
        self.assertIn("0 of 2 features added", text)
        picked = self.cad.sent[-2][2]["feature"]["parameters"][2]["queries"][0]["queryString"]
        self.assertIn("vector(0.0000000, 0.0590000, 0.0000000) * meter", picked)


class MoreBuildingTest(unittest.TestCase):
    """Planes, fillets, chamfers, part names; and every write pinned to the version the last one left."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ctx, self.cad = context(self.dir.name)
        tools.read(self.ctx, TAB)

    def tearDown(self):
        self.dir.cleanup()

    STEPS = [
        {"plane": {"name": "Side face", "from": "Right", "offset": "#thickness * 2", "flip": True}},
        {"sketch": dict(PLATE["sketch"], name="Side outline", plane="Side face")},
        {"extrude": {"name": "Side panel", "sketch": "Side outline", "depth": "#thickness", "regions": [[40, 20]], "part": "Left side panel"}},
        {"fillet": {"name": "Rounded corners", "radius": "5 mm", "edges": [[-40, 0, 0], [-40, 60, 0]]}},
        {"chamfer": {"name": "Broken edge", "width": "1 mm", "edges": [[-40, 30, 40]]}},
        {"name_part": {"made_by": "Plate", "name": "Base plate"}},
    ]

    def test_dry_run_counts_the_requests_including_naming(self):
        text = tools.build(self.ctx, TAB, self.STEPS)
        self.assertIn('1. plane     "Side face": #thickness * 2 from Right, on the far side', text)
        self.assertIn('part named "Left side panel"', text)
        self.assertIn('6. name      the part made by "Plate" becomes "Base plate"', text)
        self.assertIn("Building this costs 7 requests: one per feature plus 2 to name the parts", text)

    def test_everything_is_built_in_order_on_the_right_plane_and_parts_are_named(self):
        text = tools.build(self.ctx, TAB, self.STEPS, dry_run=False)
        self.assertIn("5 of 5 features added", text)
        self.assertIn('part of "Side panel": named "Left side panel"', text)
        self.assertIn('"Side outline"  on the plane "Side face": ', tools.read(self.ctx, TAB))
        self.assertIn("Requests: 7 this call.", text)
        adds = [body for method, path, body in self.cad.sent if method == "POST" and path.endswith("/features")]
        plane, drawn, extrude, fillet, chamfer = (body["feature"] for body in adds)
        self.assertEqual([body["sourceMicroversion"] for body in adds], [str(n) * 24 for n in range(1, 6)])  # each pinned to the last
        self.assertTrue(all(body["rejectMicroversionSkew"] for body in adds))
        parameters = {p["parameterId"]: p.get("value", p.get("expression")) for p in plane["parameters"]}
        self.assertEqual((parameters["cplaneType"], parameters["offset"], parameters["oppositeDirection"]), ("OFFSET", "#thickness * 2", True))
        new_plane = self.cad.features[-5]["featureId"]
        self.assertIn(f'makeId("{new_plane}")', drawn["parameters"][0]["queries"][0]["queryString"])
        picked = extrude["parameters"][2]["queries"][0]["queryString"]
        self.assertIn("vector(-0.0400000, 0.0400000, 0.0200000) * meter", picked)  # sketch (40, 20) on the plane 40 mm behind Right
        self.assertIn("EntityType.EDGE", fillet["parameters"][0]["queries"][0]["queryString"])
        self.assertEqual(chamfer["parameters"][1]["value"], "EQUAL_OFFSETS")
        self.assertEqual(sorted(self.cad.names.values()), ["Base plate", "Left side panel"])

    def test_a_build_is_refused_when_the_owner_edited_after_the_read(self):
        self.cad.edit_by_owner()
        with self.assertRaisesRegex(RuntimeError, "changed after it was read"):
            tools.build(self.ctx, TAB, self.STEPS[:1], dry_run=False)
        self.assertEqual(len(self.cad.features), 4)

    def test_bad_new_steps_are_refused(self):
        with self.assertRaises(ValueError) as raised:
            tools.build(self.ctx, TAB, [{"plane": {"name": "P", "from": "Left", "offset": "5 mm"}},
                                        {"plane": {"name": "Q", "from": "Top", "offset": "-5 mm"}},
                                        {"fillet": {"name": "R", "radius": "5 mm", "edges": [[1, 2]]}},
                                        {"extrude": {"name": "Cut", "sketch": "Layout", "depth": "2 mm", "operation": "remove", "part": "X"}},
                                        {"name_part": {"made_by": "Nothing", "name": "X"}},
                                        {"sketch": dict(PLATE["sketch"], plane="No such plane")}])
        text = str(raised.exception)
        for why in ("offset \"from\" Top, Front or Right", "use \"flip\": true", "edges must be a list of [x, y, z] points",
                    "only changes a part", "there is no feature \"Nothing\"", "No feature 'No such plane'"):
            self.assertIn(why, text)


class EditabilityTest(unittest.TestCase):
    """Reading how editable a model is, and repairing what can be repaired."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ctx, self.cad = context(self.dir.name)
        tools.read(self.ctx, TAB)
        tools.build(self.ctx, TAB, [PLATE], dry_run=False)  # a sketch as this tool writes it
        self.sketch = self.cad.features[-1]

    def tearDown(self):
        self.dir.cleanup()

    def strip_labels(self):
        for constraint in self.sketch["constraints"]:
            constraint["parameters"] = [p for p in constraint["parameters"] if not p["parameterId"].startswith("label")]

    def test_read_says_whether_each_sketch_is_fully_defined_and_what_can_move(self):
        text = tools.read(self.ctx, TAB)
        self.assertIn('"Bracket outline"  on the Top plane: 1 point, 4 lines, 1 circle (1 construction), 15 constraints; fully defined', text)
        self.sketch["constraints"] = self.sketch["constraints"][:-2]  # the owner deletes the two dimensions that place the hole
        self.cad.edit_by_owner()
        text = tools.read(self.ctx, TAB)
        self.assertIn("NOT fully defined: 2 movements still free (circle 1: centre x, centre y)", text)
        self.assertIn("can still move: circle 1: centre x, centre y", tools.inspect(self.ctx, TAB, "Bracket outline"))
        self.assertIn('"Layout"  on the Top plane: 3 circles, 1 line, 8 constraints; not assessed here (it uses', text)  # said, not guessed

    def test_hidden_dimensions_are_reported_then_repaired_one_request_per_sketch(self):
        self.strip_labels()
        self.cad.edit_by_owner()
        self.assertIn("5 dimensions have no label, so the sketch editor may not show them", tools.read(self.ctx, TAB))
        plan = tools.repair(self.ctx, TAB)
        self.assertIn('5 "Bracket outline": 5 dimensions get a label the sketch editor can show', plan)
        self.assertIn("Applying this costs 1 requests", plan)
        sent = len(self.cad.sent)
        done = tools.repair(self.ctx, TAB, dry_run=False)
        self.assertIn("The repaired sketches rebuild: 1 OK", done)
        self.assertEqual(len(self.cad.sent), sent + 1)
        self.assertIn("/features/featureid/", self.cad.sent[-1][1])
        self.assertNotIn("no label", tools.read(self.ctx, TAB))
        self.assertIn("Nothing to repair", tools.repair(self.ctx, TAB))

    def test_a_changed_dimension_is_solved_here_and_sent_with_exact_coordinates(self):
        tools.read(self.ctx, TAB)
        text = tools.set_numbers(self.ctx, TAB, [{"feature": "Bracket outline", "number": "length(line 1)", "to": "80 mm"},
                                                 {"feature": "#thickness", "number": "#thickness", "to": "30 mm"}], dry_run=False)
        self.assertIn('"Bracket outline" was re-solved here: its shapes move by up to 20.000 mm.', text)
        sent = next(f for f in self.cad.sent[-1][2]["features"] if f["featureType"] == "newSketch") if "features" in self.cad.sent[-1][2] \
            else next(f for f in self.cad.sent[-2][2]["features"] if f["featureType"] == "newSketch")
        hole = next(e for e in sent["entities"] if e["entityId"] == "hole")["geometry"]
        bottom = next(e for e in sent["entities"] if e["entityId"] == "plate.bottom")
        self.assertAlmostEqual(hole["yCenter"], 0.030)  # the hole follows the variable
        self.assertAlmostEqual(bottom["endParam"], 0.080)  # the plate is 80 long

    def test_a_value_the_sketch_cannot_take_is_refused_before_sending(self):
        self.sketch["constraints"].append(  # the owner also dimensions the top edge: now the plate cannot be 95 long
            {"btType": "BTMSketchConstraint-2", "constraintType": "LENGTH", "entityId": "extra",
             "parameters": [{"btType": "BTMParameterString-149", "parameterId": "localFirst", "value": "plate.top"},
                            {"btType": "BTMParameterQuantity-147", "parameterId": "length", "expression": "60 mm"}]})
        self.cad.edit_by_owner()
        self.assertIn("fully defined; 1 constraint repeats others", tools.read(self.ctx, TAB))
        with self.assertRaisesRegex(ValueError, "cannot be solved"):
            tools.set_numbers(self.ctx, TAB, [{"feature": "Bracket outline", "number": "length(line 1)", "to": "95 mm", "was": "60 mm"}])


class FileBackendTest(unittest.TestCase):
    def test_a_mesh_file_is_checked_and_pictured_for_free_and_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, cad = context(tmp)
            path = Path(tmp) / "spacer.stl"
            path.write_bytes(meshes.tube(10, 6, 20, (0, 0, 0)))
            text = tools.check(ctx, str(path))
            self.assertIn("1 body, 4.02 cm3 in total, overall 20.00 x 20.00 x 20.00 mm", text)
            self.assertIn("1 x boss diameter 20.00 along Z, 20.00 long, centres (x, y): (0.00, 0.00)", text)
            self.assertIn("1 x hole diameter 12.00 along Z", text)
            self.assertIn("Requests: 0 this call.", text)
            answer = tools.view(ctx, str(path))
            self.assertEqual(answer.png[:4], b"\x89PNG")
            self.assertIn("view.png", answer.text)
            path.write_bytes(meshes.tube(10, 8, 20, (0, 0, 0)))  # the file changes: a new version, and the difference is reported
            self.assertIn("volume 4.02 -> 2.26 cm3", tools.check(ctx, str(path)))
            with self.assertRaisesRegex(RuntimeError, "The file backend cannot change numbers"):
                tools.set_numbers(ctx, str(path), [{"feature": "1", "number": "depth", "to": "2 mm"}])
            self.assertEqual(cad.sent, [])


class McpServerTest(unittest.TestCase):
    def talk(self, ctx, *messages):
        out = io.BytesIO()
        serve(lambda: ctx, io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages)), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_handshake_tools_resources_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies = self.talk(
                context(tmp)[0],
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cad_read", "arguments": {"model": TAB}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "cad_read", "arguments": {"model": "nonsense"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "cad_read", "arguments": {"url": TAB}}},
                {"jsonrpc": "2.0", "id": 6, "method": "resources/list"},
                {"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": {"uri": "guide://editable-models"}},
                {"jsonrpc": "2.0", "id": 8, "method": "resources/read", "params": {"uri": "guide://../cli"}},
                {"jsonrpc": "2.0", "id": 9, "method": "no/such"},
                {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                 "params": {"name": "cad_build", "arguments": {"model": TAB, "steps": [PLATE, EXTRUDE]}}},
            )
        by_id = {r["id"]: r for r in replies}
        self.assertEqual(len(replies), 10)  # the notification gets no answer
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn(GOAL, by_id[1]["result"]["instructions"])
        self.assertEqual([t["name"] for t in by_id[2]["result"]["tools"]],
                         ["cad_status", "cad_read", "cad_inspect", "cad_check", "cad_view", "cad_motion", "cad_set", "cad_build", "cad_repair"])
        self.assertEqual(by_id[2]["result"]["tools"][1]["inputSchema"]["required"], ["model"])
        self.assertFalse(by_id[3]["result"]["isError"])
        self.assertIn("Feature tree at version", by_id[3]["result"]["content"][0]["text"])
        self.assertTrue(by_id[4]["result"]["isError"])
        self.assertIn("Not an Onshape tab link", by_id[4]["result"]["content"][0]["text"])
        self.assertEqual(by_id[5]["error"]["code"], -32602)
        self.assertEqual([r["uri"] for r in by_id[6]["result"]["resources"]],
                         ["guide://agent-quickstart", "guide://editable-models"])  # in reading order
        self.assertIn("fully defined", by_id[7]["result"]["contents"][0]["text"])
        self.assertIn("error", by_id[8])
        self.assertEqual(by_id[9]["error"]["code"], -32601)
        self.assertIn("Dry run: nothing was sent", by_id[10]["result"]["content"][0]["text"])  # writing is opt-in

    def test_a_picture_comes_back_as_an_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spacer.stl"
            path.write_bytes(meshes.tube(10, 6, 20, (0, 0, 0)))
            (reply,) = self.talk(context(tmp)[0], {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                   "params": {"name": "cad_view", "arguments": {"model": str(path), "source": "live"}}})
        kinds = [part["type"] for part in reply["result"]["content"]]
        self.assertEqual(kinds, ["text", "image"])
        self.assertEqual(reply["result"]["content"][1]["mimeType"], "image/png")

    def test_the_real_entry_point_starts_and_answers_without_any_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ["PATH"], "PYTHONPATH": SRC, "HOME": tmp, "ONSHAPE_ACCESS_KEY": "x", "ONSHAPE_SECRET_KEY": "y"}
            messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cad_status", "arguments": {}}}]
            done = subprocess.run([sys.executable, "-m", "onshape_mcp", "serve"], cwd=tmp, env=env, capture_output=True, timeout=60,
                                  input=b"".join(json.dumps(m).encode() + b"\n" for m in messages))
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "onshape-mcp")
        self.assertIn("Budget: project 'default' 0/0", replies[1]["result"]["content"][0]["text"])
        self.assertNotIn("NOT READY", replies[1]["result"]["content"][0]["text"])

    def test_without_keys_it_still_starts_says_so_and_measures_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            mesh = Path(tmp) / "spacer.stl"
            mesh.write_bytes(meshes.tube(10, 6, 20, (0, 0, 0)))
            env = {"PATH": os.environ["PATH"], "PYTHONPATH": SRC, "HOME": tmp}
            run = lambda *a: subprocess.run([sys.executable, "-m", "onshape_mcp", *a], cwd=tmp, env=env,  # noqa: E731
                                            capture_output=True, timeout=60, text=True)
            status, check, read = run("status"), run("check", str(mesh)), run("read", TAB)
            ledgers = list(Path(tmp).rglob("ledger.json"))
        self.assertEqual((status.returncode, check.returncode, read.returncode), (0, 0, 1))
        self.assertIn("onshape (links; NOT READY: No Onshape API keys.", status.stdout)
        self.assertIn("1 x hole diameter 12.00 along Z", check.stdout)
        self.assertIn("No Onshape API keys", read.stderr)
        self.assertEqual(ledgers, [])  # the refused read was never counted

    def test_the_command_line_offers_every_tool_and_dry_runs_by_default(self):
        parser_help = io.StringIO()
        with self.assertRaises(SystemExit):
            sys.stdout, saved = parser_help, sys.stdout
            try:
                main(["--help"])
            finally:
                sys.stdout = saved
        for command in ("status", "read", "inspect", "check", "set", "build", "serve", "guide"):
            self.assertIn(command, parser_help.getvalue())


class AgentInstructionsTest(unittest.TestCase):
    """What an AI agent is told, over MCP and from a shell, must match the tools that exist."""

    def said(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        sys.stdout, sys.stderr, saved = out, err, (sys.stdout, sys.stderr)
        try:
            status = main(list(arguments))
        finally:
            sys.stdout, sys.stderr = saved
        return status, out.getvalue(), err.getvalue()

    def test_the_guide_command_prints_the_loop_then_every_guide_and_needs_no_settings(self):
        status, text, _ = self.said("guide")
        self.assertEqual(status, 0)
        self.assertTrue(text.startswith("Goal: " + GOAL))
        self.assertLess(text.index("# How an AI agent uses these tools"), text.index("# Building a model a person can keep editing"))
        status, text, _ = self.said("guide", "editable-models")
        self.assertEqual((status, text.splitlines()[0]), (0, "# Building a model a person can keep editing"))
        status, _, problem = self.said("guide", "no-such-guide")
        self.assertEqual(status, 1)
        self.assertIn("agent-quickstart, editable-models", problem)

    def test_every_tool_the_instructions_name_exists_and_every_tool_has_its_cost_listed(self):
        names = {tool.name for tool in tools.TOOLS}
        quickstart = (GUIDES / "agent-quickstart.md").read_text()
        for text in [INSTRUCTIONS, (ROOT / "README.md").read_text(), (ROOT / "AGENTS.md").read_text()] + [p.read_text() for p in guides()]:
            self.assertLessEqual(set(re.findall(r"\bcad_[a-z]+\b", text)), names)
        costs = quickstart[quickstart.index("## What each tool costs"):quickstart.index("## What to pass")]
        self.assertEqual(re.findall(r"^\| `(cad_[a-z]+)` \|", costs, re.M), [tool.name for tool in tools.TOOLS])
        for uri in re.findall(r"guide://[a-z-]+", INSTRUCTIONS):
            self.assertTrue((GUIDES / (uri.removeprefix("guide://") + ".md")).is_file(), uri)


if __name__ == "__main__":
    unittest.main()
