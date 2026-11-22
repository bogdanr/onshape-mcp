"""The Onshape backend, against a scripted transport and a fixture cut from real Onshape responses."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from onshape_mcp.backends.base import Snapshot
from onshape_mcp.backends.onshape import outline
from onshape_mcp.backends.onshape.backend import OnshapeBackend
from onshape_mcp.backends.onshape.client import Credentials, OnshapeClient, OnshapeError, load_credentials
from onshape_mcp.budget import BudgetExceeded, Ledger
from onshape_mcp.config import ConfigError

FIXTURE = Path(__file__).parent / "fixtures" / "onshape_features.json"
TAB = "https://cad.onshape.com/documents/aaaaaaaaaaaaaaaaaaaaaaaa/w/bbbbbbbbbbbbbbbbbbbbbbbb/e/cccccccccccccccccccccccc"


class Scripted:
    """A transport that answers from a list and records what it was sent."""

    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def __call__(self, method, url, headers, body):
        self.sent.append((method, url, headers, body))
        return self.answers.pop(0)


def client(tmp, transport, budget=10):
    return OnshapeClient(Credentials("ACCESS", "SECRET"), Ledger(Path(tmp) / "ledger.json", "t", budget),
                         transport=transport)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def test_every_redirect_hop_is_counted_and_keys_stay_on_onshape(self):
        transport = Scripted(
            (307, {"Location": "https://cad-usw2.onshape.com/modelexport?id=1"}, b""),  # a regional Onshape server
            (307, {"location": "https://files.example.net/blob"}, b""),
            (200, {}, b"STL"),
        )
        c = client(self.tmp, transport)
        self.assertEqual(c.request("GET", "/partstudios/x/stl", query={"mode": "binary"}, accept="*/*"), b"STL")
        self.assertEqual(c.ledger.status()["used"], 3)
        self.assertEqual(transport.sent[0][1], "https://cad.onshape.com/api/v10/partstudios/x/stl?mode=binary")
        self.assertIn("Authorization", transport.sent[1][2])
        self.assertNotIn("Authorization", transport.sent[2][2])

    def test_keys_are_not_sent_to_lookalike_hosts_or_over_plain_http(self):
        for target in ("https://evilonshape.com/x", "http://cad.onshape.com/x", "https://onshape.com.example.net/x"):
            transport = Scripted((307, {"Location": target}, b""), (401, {}, b"denied"))
            with self.assertRaises(OnshapeError) as raised:
                client(self.tmp, transport).request("GET", "/y")
            self.assertNotIn("Authorization", transport.sent[1][2], target)
            self.assertIn("the keys were not sent", str(raised.exception))

    def test_failures_are_counted_never_retried_and_keep_onshapes_words(self):
        transport = Scripted((400, {}, b'{"message": "Parameter depth is invalid"}'))
        c = client(self.tmp, transport)
        with self.assertRaises(OnshapeError) as raised:
            c.request("POST", "/partstudios/x/features", body={"feature": {}})
        self.assertIn("Parameter depth is invalid", str(raised.exception))
        self.assertEqual((len(transport.sent), c.ledger.status()["used"]), (1, 1))

    def test_a_redirected_write_is_not_resent(self):
        transport = Scripted((307, {"Location": "https://cad.onshape.com/elsewhere"}, b""))
        with self.assertRaises(OnshapeError):
            client(self.tmp, transport).request("POST", "/x", body={})
        self.assertEqual(len(transport.sent), 1)

    def test_nothing_is_sent_once_the_budget_is_used(self):
        transport = Scripted((200, {}, b"{}"))
        c = client(self.tmp, transport, budget=1)
        c.get_json("/a")
        with self.assertRaises(BudgetExceeded):
            c.get_json("/b")
        self.assertEqual(len(transport.sent), 1)

    def test_credentials_from_env_json_lines_or_a_sentence_and_never_shown(self):
        path = Path(self.tmp) / "keys"
        for text in ('{"access_key": "AAA111", "secret_key": "SSS222"}', "ACCESS_KEY=AAA111\nSECRET_KEY=SSS222\n",
                     "My access key is AAA111 and the secret key is SSS222."):
            path.write_text(text)
            found = load_credentials(str(path), env={})
            self.assertEqual((found.access_key, found.secret_key), ("AAA111", "SSS222"), text)
        self.assertNotIn("SSS222", repr(found))
        self.assertEqual(load_credentials(None, env={"ONSHAPE_ACCESS_KEY": "E1", "ONSHAPE_SECRET_KEY": "E2"}).access_key, "E1")
        path.write_text("only an access key is AAA111")
        with self.assertRaises(ConfigError) as raised:
            load_credentials(str(path), env={})
        self.assertNotIn("AAA111", str(raised.exception))


class BackendTest(unittest.TestCase):
    def test_reads_the_tree_in_one_request_and_pins_the_mesh_to_that_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = Scripted((200, {}, FIXTURE.read_bytes()), (200, {}, b"MESH"))
            backend = OnshapeBackend(client(tmp, transport))
            ref = backend.parse_ref(TAB + "?renderMode=0")
            snapshot = backend.read_studio(ref)
            self.assertEqual(snapshot.version, "0123456789abcdef01234567")
            self.assertEqual(backend.export_mesh(ref, snapshot.version), b"MESH")
            self.assertTrue(transport.sent[0][1].endswith("/d/" + "a" * 24 + "/w/" + "b" * 24 + "/e/" + "c" * 24 + "/features"))
            self.assertIn("/m/0123456789abcdef01234567/e/", transport.sent[1][1])
            self.assertIn("units=millimeter", transport.sent[1][1])
            with self.assertRaises(ValueError):
                backend.parse_ref("https://cad.onshape.com/documents/" + "a" * 24)


class EvaluationTest(unittest.TestCase):
    def test_a_script_that_fails_is_an_error_with_onshapes_words_never_an_empty_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            failed = json.dumps({"result": None, "notices": [{"message": "Identifier expected, found keyword box", "level": "ERROR"}]}).encode()
            backend = OnshapeBackend(client(tmp, Scripted((200, {}, failed))))
            with self.assertRaisesRegex(OnshapeError, "returned nothing: Identifier expected, found keyword box"):
                backend.part_boxes(backend.parse_ref(TAB))

    def test_scripts_do_not_use_featurescript_keywords_as_names(self):
        from onshape_mcp.backends.onshape import backend as module
        reserved = {"box", "new", "export", "import", "enum", "type", "predicate", "operator", "annotation", "precondition", "typecheck"}
        for script in (module.STATUS_SCRIPT, module.BODIES_SCRIPT, module.PARTS_SCRIPT):
            declared = set(re.findall(r"(?:var|const)\s+(\w+)", script))
            self.assertFalse(declared & reserved, declared & reserved)


class OutlineTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot(None, "v", json.loads(FIXTURE.read_text()))
        self.items = outline.outline(self.snapshot)

    def test_one_line_per_feature_with_the_numbers_a_person_would_edit(self):
        variable, sketch, extrude, transform = self.items
        self.assertEqual((variable.kind, variable.name, variable.numbers), ("variable", "#thickness", [("#thickness", "20 mm")]))
        self.assertEqual(sketch.kind, "sketch")
        self.assertIn("on the Top plane: 3 circles, 1 line", sketch.notes[0])
        self.assertIn(("diameter(circle 1)", "180 mm"), sketch.numbers)
        self.assertIn(("distance(circle 2, circle 1)", "2 mm"), sketch.numbers)
        self.assertEqual(extrude.numbers, [("depth", "1 mm")])  # 1 of about 45 stored parameters
        self.assertEqual(extrude.notes, ["NEW", "BLIND"])
        self.assertEqual((transform.status, transform.numbers), ("ERROR", [("angle", "37 deg")]))

    def test_inspect_finds_a_feature_by_position_name_or_id(self):
        self.assertIn("circle 2   centre (0, 58.6) radius 29.4", outline.describe(self.snapshot, "2"))
        self.assertIn("vertical(circle 1.center, circle 2.center)", outline.describe(self.snapshot, "layout"))
        self.assertIn("coincident(circle 1.center, outside geometry)", outline.describe(self.snapshot, "Layout"))
        self.assertIn("depth = 1 mm", outline.describe(self.snapshot, "Fextrude00000001_0"))
        with self.assertRaises(LookupError):
            outline.describe(self.snapshot, "no such feature")


if __name__ == "__main__":
    unittest.main()
