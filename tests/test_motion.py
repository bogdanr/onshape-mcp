"""Assemblies: what moves when a joint turns, and how close the parts come.

The sweep needs numpy and scipy. It runs here if this Python has them, or on the machine named by
ONSHAPE_MCP_TEST_SSH_HOST; otherwise those tests are skipped.
"""
import os
import tempfile
import unittest

import meshes
from fake_onshape import context

from onshape_mcp import tools
from onshape_mcp.backends.base import ModelRef
from onshape_mcp.motion import Assembly, Instance, Joint, split
from onshape_mcp.runners.local import LocalRunner
from onshape_mcp.runners.ssh import SshRunner

LINK = "https://cad.onshape.com/documents/aaaaaaaaaaaaaaaaaaaaaaaa/w/bbbbbbbbbbbbbbbbbbbbbbbb/e/dddddddddddddddddddddddd"
SAME = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
CS = {"xAxis": [0, 1, 0], "yAxis": [0, 0, 1], "zAxis": [1, 0, 0]}


def sweep_runner():
    try:
        import scipy  # noqa: F401
        return LocalRunner()
    except ImportError:
        host = os.environ.get("ONSHAPE_MCP_TEST_SSH_HOST")
        return SshRunner(host, python=os.environ.get("ONSHAPE_MCP_TEST_SSH_PYTHON", "python3")) if host else None


def scene(cad):
    """A base plate, a pin on the axis, and an arm 10 mm above the plate that turns on the pin.

    In its studio the arm is drawn 200 mm away; the assembly moves it into place. Turning it down, its far end
    reaches the plate at 7.2 degrees.
    """
    base, pin = meshes.box((0, 0, 0), (100, 100, 10)), meshes.tube(4, 2, 20, (0, 25, 40), axis="x")
    arm = meshes.box((245, 0, 20), (255, 80, 30))
    wall = meshes.box((0, 100, 0), (100, 110, 40))  # shares a whole edge and two corners with the base, as boards do
    cad.mesh = meshes.merge(base, pin, arm, wall)
    cad.parts = [[["JHD"], [0, 0, 0], [0.1, 0.1, 0.01], 1e-4, "Base plate"], [["JKD"], [0.04, -0.004, 0.021], [0.06, 0.004, 0.029], 1e-6, "Pin"],
                 [["JLD"], [0.245, 0, 0.02], [0.255, 0.08, 0.03], 8e-6, "Arm"], [["JMD"], [0, 0.1, 0], [0.1, 0.11, 0.04], 4e-5, "Back wall"]]
    def part(key, name, part_id):
        return {"id": key, "name": name, "type": "Part", "suppressed": False, "partId": part_id, "documentId": "a" * 24,
                "elementId": "c" * 24, "documentMicroversion": "7" * 24}
    moved = list(SAME)
    moved[3] = -0.2
    cad.assembly = {"subAssemblies": [], "rootAssembly": {
        "documentMicroversion": "7" * 24,
        "instances": [part("Mbase", "Base plate <1>", "JHD"), part("Mpin", "Pin <1>", "JKD"), part("Marm", "Arm <1>", "JLD"),
                      part("Mwall", "Back wall <1>", "JMD")],
        "occurrences": [{"path": ["Mbase"], "fixed": True, "transform": SAME}, {"path": ["Mpin"], "fixed": False, "transform": SAME},
                        {"path": ["Marm"], "fixed": False, "transform": moved}, {"path": ["Mwall"], "fixed": False, "transform": SAME}],
        "features": [
            {"featureType": "mateGroup", "featureData": {"name": "Frame", "occurrences": [
                {"occurrence": ["Mbase"]}, {"occurrence": ["Mpin"]}, {"occurrence": ["Mwall"]}]}},
            {"featureType": "mate", "featureData": {"name": "Hinge", "mateType": "REVOLUTE", "matedEntities": [
                {"matedOccurrence": ["Mpin"], "matedCS": dict(CS, origin=[0.05, 0, 0.025])},
                {"matedOccurrence": ["Marm"], "matedCS": dict(CS, origin=[0.25, 0, 0.025])}]}}]}}


class KinematicsTest(unittest.TestCase):
    def assembly(self):
        ref = ModelRef("x", "d", "m/1", "e")
        parts = {name: Instance(name, name, ref, name, SAME, fixed=(name == "ground")) for name in ("ground", "rocker", "motor", "tube", "mirror", "spare")}
        return Assembly(parts, [("Rocker", ["rocker", "motor"]), ("Tube", ["tube", "mirror"])],
                        [Joint("Azimuth", "revolute", ("ground", "rocker"), (0, 0, 0), (0, 0, 1)),
                         Joint("Altitude", "revolute", ("rocker", "tube"), (0, 0, 600), (1, 0, 0))])

    def test_everything_past_the_joint_moves_and_the_rest_stands_still(self):
        a = self.assembly()
        moving, still, notes = split(a, a.find_joint("alt"))
        self.assertEqual((sorted(moving), sorted(still)), (["mirror", "tube"], ["ground", "motor", "rocker", "spare"]))
        self.assertIn("1 part(s) are not held by any joint", notes[0])
        moving, _, _ = split(a, a.find_joint("1"))
        self.assertEqual(sorted(moving), ["mirror", "motor", "rocker", "tube"])  # the tube rides on the rocker

    def test_an_assembly_that_cannot_be_judged_says_why(self):
        a = self.assembly()
        a.instances["ground"].fixed = False
        with self.assertRaisesRegex(ValueError, "Nothing in this assembly is fixed"):
            split(a, a.joints[0])
        a.instances["ground"].fixed = True
        a.groups.append(("Welded", ["rocker", "tube"]))
        with self.assertRaisesRegex(ValueError, "cannot turn"):
            split(a, a.joints[1])
        with self.assertRaisesRegex(LookupError, "Joints: 1 \"Azimuth\"; 2 \"Altitude\""):
            a.find_joint("elevation")


class MotionToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ctx, self.cad = context(self.dir.name)
        scene(self.cad)

    def tearDown(self):
        self.dir.cleanup()

    def test_listing_the_joints_costs_one_request_and_places_the_axis(self):
        text = tools.motion(self.ctx, LINK)
        self.assertIn("4 parts, 1 rigid groups, 1 joints that move. Fixed: Base plate <1>.", text)
        self.assertIn('1. revolute "Hinge": "Pin <1>" to "Arm <1>", axis through (50.00, 0.00, 25.00) mm along (1.000, 0.000, 0.000)', text)
        self.assertIn("Requests: 1 this call.", text)

    def test_a_joint_whose_sides_do_not_meet_is_flagged(self):
        self.cad.assembly["rootAssembly"]["features"][1]["featureData"]["matedEntities"][1]["matedCS"]["origin"] = [0.25, 0, 0.030]
        self.assertIn("its two sides are 5.00 mm apart: the joint is not solved", tools.motion(self.ctx, LINK))

    @unittest.skipUnless(sweep_runner(), "needs numpy and scipy here, or ONSHAPE_MCP_TEST_SSH_HOST")
    def test_the_sweep_finds_the_collision_its_angle_and_the_joint_geometry(self):
        self.ctx.runner = sweep_runner()
        text = tools.motion(self.ctx, LINK, joint="Hinge", from_deg=-15, to_deg=0, step_deg=1)
        self.assertIn("Requests: 4 this call.", text)  # the assembly, the mesh (2), the part list
        self.assertIn('the side of "Pin <1>" (3 parts held together): the axis is the centre of round geometry of "Pin <1>" (diameter 8.00, diameter 4.00)', text)
        self.assertIn('the side of "Arm <1>": NO round geometry is centred on the axis', text)
        self.assertIn("Moving: 1 parts. Standing still: 3 parts.", text)
        self.assertNotIn("NOT MEASURED", text)  # the wall and the base touch, yet each is found
        self.assertRegex(text, r'(19\.\d\d|20\.[0-4]\d) mm at 0\.00 deg: "Arm <1>" and "Back wall <1>"')
        self.assertIn('touching at every position: "Arm <1>" and "Pin <1>"', text)
        self.assertRegex(text, r'COLLIDES or touches \(closer than 0\.6 mm\) from -15\.00 to -[78]\.00 deg: "Arm <1>" and "Base plate <1>"')
        up = tools.motion(self.ctx, LINK, joint="Hinge", from_deg=0, to_deg=10, step_deg=5, source="cached")
        self.assertIn("Requests: 0 this call.", up)
        self.assertRegex(up, r'(9\.[5-9]\d|10\.[0-4]\d) mm at 0\.00 deg: "Arm <1>" and "Base plate <1>"')  # 10 mm above it, at rest


if __name__ == "__main__":
    unittest.main()
