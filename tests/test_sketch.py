"""Expressions and the sketch gate."""
import copy
import math
import unittest

from onshape_mcp import expr, sketch
from onshape_mcp.backends.onshape import build
from onshape_mcp.expr import Quantity

VARIABLES = {"thickness": Quantity(20.0, "length"), "count": Quantity(5, "number")}
SPEC = {"name": "Plate", "plane": "Top", "entities": [
    {"id": "plate", "rectangle": [-40, -25, 40, 25]},
    {"id": "hole", "circle": [20, 0, 4]},
    {"id": "slot", "arc": [-20, 0, 6, 0, 180]},
    {"id": "base", "line": [-26, 0, -14, 0]},
], "constraints": [
    ["distance_x", "origin", "plate.bottom.start", "40 mm"],
    ["distance_y", "origin", "plate.bottom.start", "25 mm"],
    ["length", "plate.bottom", "80 mm"],
    ["length", "plate.right", "50 mm"],
    ["diameter", "hole", "8 mm"],
    ["horizontal", "hole.center", "origin"],
    ["distance_x", "origin", "hole.center", "#thickness"],
    ["coincident", "slot.start", "base.end"],
    ["coincident", "slot.end", "base.start"],
    ["horizontal", "base"],
    ["horizontal", "slot.center", "origin"],
    ["distance_x", "slot.center", "origin", "20 mm"],
    ["radius", "slot", "6 mm"],
    ["coincident", "slot.center", "base"],
]}


def gate(change=None):
    spec = copy.deepcopy(SPEC)
    if change:
        change(spec)
    drawn = sketch.from_spec(spec, VARIABLES)
    return sketch.check(drawn), drawn


class ExpressionTest(unittest.TestCase):
    def test_values_as_a_cad_dialog_reads_them(self):
        for text, value, kind in (("58.8 mm", 58.8, "length"), ("206/2 mm", 103, "length"), ("(5/16) in", 7.9375, "length"),
                                  ("#thickness / 2 + 3 mm", 13, "length"), ("-(2 cm)", -20, "length"), ("2 * 3 mm", 6, "length"),
                                  ("90 deg", math.pi / 2, "angle"), ("#count * 2", 10, "number"), ("#thickness / (4 mm)", 5, "number")):
            result = expr.evaluate(text, VARIABLES)
            self.assertAlmostEqual(result.value, value, msg=text)
            self.assertEqual(result.kind, kind, text)

    def test_anything_unclear_is_refused_with_the_reason(self):
        for text, why in (("20", "needs a length unit"), ("5 mm + 3 deg", "adds a length to an angle"), ("#nope", "no variable #nope"),
                          ("3 furlongs", "not a unit"), ("(2 mm", "bracket is not closed"), ("2 mm )", "unexpected"),
                          ("sqrt(4) mm", "not a unit"), ("1 / 0 mm", "divides by zero"), ("", "ends too soon"),
                          ("__import__('os')", "unexpected")):
            with self.assertRaisesRegex(expr.ExpressionError, why, msg=text):
                expr.length(text, VARIABLES)


class GateTest(unittest.TestCase):
    def test_a_fully_defined_sketch_passes(self):
        result, _ = gate()
        self.assertTrue(result.passed)
        self.assertEqual(result.lines(), ["Fully defined: 0 free movements, 0 repeated constraints."])

    def test_missing_dimensions_are_counted_and_located(self):
        def loosen(spec):
            spec["constraints"] = [c for c in spec["constraints"] if c[1] != "hole" and c[:2] != ["distance_x", "slot.center"]]
        result, _ = gate(loosen)
        self.assertFalse(result.passed)
        self.assertEqual(result.free, 2)  # the hole's size, and the slot sliding sideways with its base line
        self.assertEqual(result.moving, {"circle 'hole'": ["radius"], "arc 'slot'": ["centre x"],
                                         "line 'base'": ["start x", "end x"]})

    def test_repeated_constraints_are_named(self):
        result, _ = gate(lambda spec: spec["constraints"].extend([["length", "plate.top", "80 mm"], ["vertical", "plate.left"]]))
        self.assertEqual(result.repeated, ["length(plate.top) = 80 mm", "vertical(plate.left)"])
        self.assertFalse(result.passed)

    def test_a_slightly_off_drawing_is_solved_exactly_before_sending(self):
        def nudge(spec):
            spec["entities"][1]["circle"] = [20.3, 0.2, 4.1]
        result, drawn = gate(nudge)
        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.moved, 0.3, places=6)
        for got, exact in zip(drawn.entities["hole"].params, (20, 0, 4)):
            self.assertAlmostEqual(got, exact, places=7)

    def test_a_drawing_far_from_its_dimensions_or_contradicting_itself_is_refused(self):
        def misplace(spec):
            spec["entities"][1]["circle"] = [30, 5, 4]
        result, _ = gate(misplace)
        self.assertEqual([text for text, _ in result.off], ["horizontal(hole.center, origin)", "distance_x(origin, hole.center) = #thickness"])
        result, _ = gate(lambda spec: spec["constraints"].append(["length", "plate.top", "70 mm"]))
        self.assertFalse(result.passed)
        result, _ = gate(lambda spec: spec["constraints"].append(["perpendicular", "plate.bottom", "plate.top"]))
        self.assertFalse(result.passed)  # two parallel sides cannot also be square to each other

    def test_bad_descriptions_are_refused_with_the_reason(self):
        for change, why in (
            (lambda s: s["entities"].append({"id": "hole", "circle": [0, 0, 1]}), "two shapes are called 'hole'"),
            (lambda s: s["entities"].append({"id": "x y", "circle": [0, 0, 1]}), "needs an id"),
            (lambda s: s["entities"].append({"id": "dot", "circle": [0, 0, 0]}), "radius greater than 0"),
            (lambda s: s["entities"].append({"id": "stub", "line": [1, 1, 1, 1]}), "has no length"),
            (lambda s: s["constraints"].append(["tangent", "slot", "base"]), "unknown constraint"),
            (lambda s: s["constraints"].append(["diameter", "slot", "12 mm"]), "use radius for an arc"),
            (lambda s: s["constraints"].append(["distance_x", "hole", "origin", "5 mm"]), "takes points"),
            (lambda s: s["constraints"].append(["length", "plate.top", "80"]), "needs a length unit"),
            (lambda s: s["constraints"].append(["distance_x", "origin", "nowhere", "5 mm"]), "there is no 'nowhere'"),
            (lambda s: s["constraints"].append(["distance_y", "origin", "hole.center", "0 mm"]), "use horizontal or vertical"),
            (lambda s: s.update(plane="Side"), "plane must be one of"),
        ):
            with self.assertRaisesRegex(ValueError, why):
                gate(change)


class OnshapeSketchTest(unittest.TestCase):
    def test_distance_alignment_follows_the_solved_positions(self):
        _, drawn = gate()
        feature = build.sketch(drawn)
        constraints = [{p["parameterId"]: p.get("value", p.get("expression")) for p in c["parameters"]} | {"id": c["entityId"]}
                       for c in feature["constraints"]]
        to_corner = next(c for c in constraints if c.get("localSecond") == "plate.bottom.start" and c.get("direction") == "HORIZONTAL")
        to_hole = next(c for c in constraints if c.get("localSecond") == "hole.center" and c.get("direction") == "HORIZONTAL")
        self.assertEqual(to_corner["alignment"], "ANTI_ALIGNED")  # the plate corner is left of the origin
        self.assertEqual(to_hole["alignment"], "ALIGNED")  # the hole is right of it
        self.assertEqual(to_hole["length"], "#thickness")  # the owner sees the expression, not 20
        self.assertEqual(next(c for c in constraints if c["id"] == "origin_pin")["localFirst"], "origin")
        self.assertEqual(len({c["id"] for c in constraints}), len(constraints))
        arc = next(e for e in feature["entities"] if e["entityId"] == "slot")
        self.assertAlmostEqual(arc["endParam"] - arc["startParam"], math.pi)
        self.assertEqual(build.world("Front", 3, 4), (3, 0, 4))
        self.assertEqual(build.world("Right", 3, 4), (0, 3, 4))


if __name__ == "__main__":
    unittest.main()
