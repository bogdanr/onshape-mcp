"""Geometry jobs and the runners that execute them.

The SSH test against a real machine runs only when ONSHAPE_MCP_TEST_SSH_HOST is set, for example:
    ONSHAPE_MCP_TEST_SSH_HOST=user@cad-box ONSHAPE_MCP_TEST_SSH_PYTHON=python3 python3 -m unittest discover -s tests
"""
import base64
import math
import os
import struct
import tempfile
import unittest
from pathlib import Path

import meshes

from onshape_mcp.config import ConfigError
from onshape_mcp.jobs import mesh_report
from onshape_mcp.runners import create
from onshape_mcp.runners.base import RunnerError
from onshape_mcp.runners.local import LocalRunner
from onshape_mcp.runners.ssh import SshRunner

JOB = Path(mesh_report.__file__)


def shifted(stl: bytes, dx: float) -> bytes:
    """The triangles of an STL moved along x, without the header."""
    out = b""
    for t in struct.iter_unpack("<12fH", stl[84:]):
        v = list(t)
        for k in (3, 6, 9):
            v[k] += dx
        out += struct.pack("<12fH", *v)
    return out


class MeshReportTest(unittest.TestCase):
    def test_cube(self):
        answer = mesh_report.report(mesh_report.cube_stl(10))
        self.assertEqual(answer["bodies"], [{"volume": 1000.0, "bbox_min": [0, 0, 0], "bbox_max": [10, 10, 10],
                                            "triangles": 12, "watertight": True, "round": []}])

    def test_holes_and_bosses_are_found_with_size_axis_and_position(self):
        tube = meshes.tube(outer=10, inner=6, height=20, centre=(30, -5, 0))
        (body,) = mesh_report.report(tube)["bodies"]
        self.assertEqual(body["round"], [
            {"kind": "boss", "diameter": 20.0, "axis": [0.0, 0.0, 1.0], "center": [30.0, -5.0, 10.0], "length": 20.0},
            {"kind": "hole", "diameter": 12.0, "axis": [0.0, 0.0, 1.0], "center": [30.0, -5.0, 10.0], "length": 20.0}])
        self.assertAlmostEqual(body["volume"], math.pi * (10 ** 2 - 6 ** 2) * 20, delta=40)  # 72 flat sides read slightly small
        (lying,) = mesh_report.report(meshes.tube(10, 6, 20, (0, 0, 0), axis="x"))["bodies"]
        self.assertEqual([(f["kind"], f["diameter"], f["axis"]) for f in lying["round"]],
                         [("boss", 20.0, [1.0, 0.0, 0.0]), ("hole", 12.0, [1.0, 0.0, 0.0])])
        self.assertEqual(mesh_report.report(mesh_report.cube_stl(10))["bodies"][0]["round"], [])  # flat walls are not round

    def test_the_picture_job_draws_four_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            mesh = Path(tmp) / "tube.stl"
            mesh.write_bytes(meshes.tube(10, 6, 20, (0, 0, 0)))
            answer = LocalRunner().run(JOB.with_name("render.py"), [mesh])
        png = base64.b64decode(answer["png"])
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual((answer["width"], answer["height"]), (1280, 960))
        self.assertGreater(len(png), 5000)  # not a blank sheet

    def test_the_self_test_says_what_the_runner_has(self):
        answer = LocalRunner().run(JOB, args=["--selftest"])
        self.assertEqual(set(answer["modules"]), {"numpy", "scipy", "PIL"})
        self.assertRegex(answer["python"], r"^3\.")

    def test_two_bodies_are_told_apart_and_an_open_one_is_flagged(self):
        big, small = mesh_report.cube_stl(10), mesh_report.cube_stl(2)
        open_small = shifted(small, 50)[:-50]  # one triangle missing
        data = b"\0" * 80 + struct.pack("<I", 12 + 11) + big[84:] + open_small
        bodies = mesh_report.report(data)["bodies"]
        self.assertEqual([(b["volume"] > 100, b["watertight"]) for b in bodies], [(True, True), (False, False)])
        self.assertEqual(bodies[1]["bbox_min"][0], 50)

    def test_parts_that_touch_along_an_edge_or_a_face_stay_separate_and_closed(self):
        floor, panel = meshes.box((0, 0, -20), (100, 100, 0)), meshes.box((80, 0, 0), (100, 100, 60))  # the panel stands on the floor's edge
        lid = meshes.box((0, 0, 60), (100, 100, 70))  # and a lid touches the panel along one edge only
        bodies = mesh_report.report(meshes.merge(floor, panel, lid))["bodies"]
        self.assertEqual(sorted(b["volume"] for b in bodies), [100000.0, 120000.0, 200000.0])
        self.assertTrue(all(b["watertight"] for b in bodies))

    def test_ascii_stl_and_rubbish(self):
        text = b"solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid s\n"
        self.assertEqual(mesh_report.report(text)["triangles"], 1)
        with self.assertRaises(ValueError):
            mesh_report.report(b"not a mesh")


class RunnerTest(unittest.TestCase):
    def test_local_runner_runs_a_job_on_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            mesh = Path(tmp) / "cube.stl"
            mesh.write_bytes(mesh_report.cube_stl(5))
            self.assertEqual(LocalRunner().run(JOB, [mesh])["bodies"][0]["volume"], 125.0)

    def test_a_failing_job_says_why(self):
        with self.assertRaises(RunnerError) as raised:
            LocalRunner().run(JOB, [Path("/no/such/file.stl")])
        self.assertIn("No such file", str(raised.exception))

    def test_ssh_command_runs_in_a_throwaway_folder_and_quotes_everything(self):
        runner = SshRunner("user@box", python="~/venv/bin/python", workdir="~/jobs")
        command = runner.command("job-1", "mesh_report.py", ["my part.stl"], ["--flag"])
        self.assertEqual(command[:7], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", "user@box"])
        script = command[7]
        self.assertTrue(script.startswith("sh -c "))
        for piece in ('d="$HOME/jobs/job-1"', "rm -rf", "tar xf -", '"$HOME/venv/bin/python" mesh_report.py', "my part.stl", "--flag"):
            self.assertIn(piece, script)

    def test_settings_choose_the_runner_and_bad_ones_are_refused(self):
        self.assertIsInstance(create({"type": "local"}), LocalRunner)
        self.assertIsInstance(create({"type": "ssh", "host": "user@box"}), SshRunner)
        for bad in ({"type": "cloud"}, {"type": "ssh", "host": "-oProxyCommand=x"}, {"type": "ssh", "host": "u@b", "workdir": "a; rm -rf ~"},
                    {"type": "local", "colour": "blue"}):
            with self.assertRaises(ConfigError, msg=bad):
                create(bad)

    @unittest.skipUnless(os.environ.get("ONSHAPE_MCP_TEST_SSH_HOST"), "set ONSHAPE_MCP_TEST_SSH_HOST to test a real machine")
    def test_ssh_runner_on_a_real_machine(self):
        runner = SshRunner(os.environ["ONSHAPE_MCP_TEST_SSH_HOST"], python=os.environ.get("ONSHAPE_MCP_TEST_SSH_PYTHON", "python3"))
        with tempfile.TemporaryDirectory() as tmp:
            mesh = Path(tmp) / "my cube.stl"
            mesh.write_bytes(mesh_report.cube_stl(5))
            self.assertEqual(runner.run(JOB, [mesh])["bodies"][0]["volume"], 125.0)
        self.assertEqual(runner.run(JOB, args=["--selftest"])["bodies"][0]["volume"], 1000.0)


if __name__ == "__main__":
    unittest.main()
