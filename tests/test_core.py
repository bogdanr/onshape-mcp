"""Budget, configuration and snapshot store. Standard library only: python3 -m unittest discover -s tests"""
import json
import tempfile
import unittest
from pathlib import Path

from onshape_mcp import config
from onshape_mcp.backends.base import ModelRef, Snapshot
from onshape_mcp.budget import BudgetExceeded, Ledger
from onshape_mcp.store import Store


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ledger.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_counts_before_sending_and_stops_at_the_budget(self):
        ledger = Ledger(self.path, "demo", budget=2)
        ledger.charge("GET", "/a?secret=1")
        ledger.charge("POST", "/b")
        with self.assertRaises(BudgetExceeded):
            ledger.charge("GET", "/c")
        data = json.loads(self.path.read_text())
        self.assertEqual(data["used_total"], 2)
        self.assertEqual(data["projects"]["demo"], {"used": 2, "budget": 2})
        self.assertEqual([entry[2:] for entry in data["log"]], [["GET", "/a"], ["POST", "/b"]])  # no query strings

    def test_zero_budget_spends_nothing(self):
        with self.assertRaises(BudgetExceeded):
            Ledger(self.path, "demo", budget=0).charge("GET", "/a")
        self.assertFalse(self.path.exists())

    def test_yearly_limit_keeps_a_reserve_and_reads_an_existing_ledger(self):
        self.path.write_text(json.dumps({"used_total": 480, "year_limit": 500, "projects": {"old": {"used": 480, "budget": 480}}, "log": []}))
        ledger = Ledger(self.path, "demo", budget=10)
        with self.assertRaises(BudgetExceeded) as raised:
            ledger.charge("GET", "/a")
        self.assertIn("480 of 500", str(raised.exception))
        self.assertEqual(ledger.status()["year_limit"], 500)
        Ledger(self.path, "demo", budget=10, year_limit=1000).charge("GET", "/a")  # the config can state the limit
        self.assertEqual(json.loads(self.path.read_text())["projects"]["old"]["used"], 480)


class ConfigTest(unittest.TestCase):
    def test_project_file_is_found_from_a_subfolder_and_env_file_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "design" / "parts").mkdir(parents=True)
            (root / "design" / "onshape-mcp.toml").write_text(
                '[project]\nname = "scope"\nbudget = 7\n[backend]\nledger = "ledger.json"\n[runner]\ntype = "ssh"\nhost = "u@h"\n')
            (root / "override.toml").write_text('[project]\nbudget = 9\n')
            loaded = config.load(root / "design" / "parts", env={})
            self.assertEqual(loaded.section("project"), {"name": "scope", "budget": 7})
            self.assertEqual(loaded.section("backend")["ledger"], str((root / "design" / "ledger.json").resolve()))
            self.assertEqual(loaded.section("runner"), {"type": "ssh", "host": "u@h"})
            loaded = config.load(root / "design", env={"ONSHAPE_MCP_CONFIG": str(root / "override.toml")})
            self.assertEqual(loaded.section("project"), {"name": "scope", "budget": 9})

    def test_defaults_spend_nothing_and_bad_budgets_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(config.load(Path(tmp), env={}).section("project")["budget"], 0)
            (Path(tmp) / "onshape-mcp.toml").write_text('[project]\nbudget = "lots"\n')
            with self.assertRaises(config.ConfigError):
                config.load(Path(tmp), env={})


class StoreTest(unittest.TestCase):
    def test_latest_and_previous_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, ref = Store(Path(tmp)), ModelRef("onshape", "doc", "w/1", "tab")
            self.assertIsNone(store.latest(ref))
            for version in ("v1", "v1", "v2", "v1"):
                store.save(Snapshot(ref, version, {"features": [version]}))
            self.assertEqual(store.versions(ref), ["v1", "v2", "v1"])
            self.assertEqual(store.latest(ref).data, {"features": ["v1"]})
            self.assertEqual(store.previous(ref, "v1"), "v2")
            self.assertEqual(store.previous(ref, "v2"), "v1")
            self.assertIsNone(store.previous(ref, "v9"))


if __name__ == "__main__":
    unittest.main()
