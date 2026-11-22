"""A stand-in for Onshape's servers, so the real client, backend and ledger run in tests without the network.

It answers the handful of calls the backend makes, in the shapes real Onshape answers have, and records every
request it received.
"""
import json
from pathlib import Path

from onshape_mcp.backends.onshape.backend import OnshapeBackend
from onshape_mcp.backends.onshape.client import Credentials, OnshapeClient
from onshape_mcp.budget import Ledger
from onshape_mcp.config import Config
from onshape_mcp.jobs import mesh_report
from onshape_mcp.runners.local import LocalRunner
from onshape_mcp.store import Store
from onshape_mcp.tools import Context

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "onshape_features.json").read_text())
TAB = "https://cad.onshape.com/documents/aaaaaaaaaaaaaaaaaaaaaaaa/w/bbbbbbbbbbbbbbbbbbbbbbbb/e/cccccccccccccccccccccccc"


class FakeOnshape:
    def __init__(self):
        self.version, self.cube = "1" * 24, 10
        self.features = json.loads(json.dumps(FIXTURE["features"]))
        self.states = dict(FIXTURE["featureStates"])
        self.sent = []  # (method, path, body)
        self.names = {}  # part id -> name, as set through the metadata call
        self.mesh = None  # what a mesh export returns; a cube when not set
        self.assembly = None  # an assembly definition
        self.parts = []  # what the part listing returns: [[ids], lo, hi, volume, name]

    def edit_by_owner(self):
        """The owner changes something in the browser: the document moves to a new version."""
        self.version = format(int(self.version[0]) + 1, "d") * 24

    def __call__(self, method, url, headers, body):
        path = url.split("/api/v10", 1)[-1].split("?")[0]
        payload = json.loads(body) if body else None
        self.sent.append((method, path, payload))
        assert "Authorization" in headers
        if method == "GET" and path.endswith("/features"):
            return 200, {}, json.dumps(dict(FIXTURE, features=self.features, featureStates=self.states,
                                            sourceMicroversion=self.version, rollbackIndex=len(self.features))).encode()
        if method == "GET" and path.endswith("/stl"):
            return 307, {"Location": "https://cad-usw2.onshape.com/modelexport?id=1"}, b""
        if method == "GET" and "/modelexport" in url:
            return 200, {}, self.mesh or mesh_report.cube_stl(self.cube)
        if method == "GET" and path.startswith("/assemblies/"):
            return 200, {}, json.dumps(self.assembly).encode()
        if method == "POST" and path.endswith("/featurescript") and "evBox3d" in payload["script"]:
            return 200, {}, json.dumps({"result": typed(self.parts)}).encode()
        if method == "POST" and "/partstudios/" in path and "/featurescript" not in path and payload.get("rejectMicroversionSkew") \
                and payload["sourceMicroversion"] != self.version:
            return 409, {}, b'{"message": "Microversion skew detected", "status": 409}'
        if method == "POST" and "/features/featureid/" in path:
            changed = payload["feature"]
            self.features = [changed if f["featureId"] == changed["featureId"] else f for f in self.features]
            self.edit_by_owner()
            return 200, {}, json.dumps({"feature": changed, "featureState": {"featureStatus": "OK"},
                                        "sourceMicroversion": self.version}).encode()
        if method == "POST" and path.startswith("/metadata/"):
            self.names = {item["href"].rsplit("/", 1)[-1]: item["properties"][0]["value"] for item in payload["items"]}
            return 200, {}, json.dumps({"items": [{"status": "SUCCEEDED"} for _ in payload["items"]]}).encode()
        if method == "POST" and path.endswith("/featurescript") and "transientQueriesToStrings" in payload["script"]:
            text = lambda value: {"btType": "BTFSValueString", "value": value}  # noqa: E731
            entries = [{"key": text(f["featureId"]),
                        "value": {"btType": "BTFSValueArray", "value": [{"btType": "BTFSValueArray", "value": [text(f"J{n}D")]}]}}
                       for n, f in enumerate(self.features) if f["featureId"] in payload["script"] and f["featureType"] == "extrude"]
            return 200, {}, json.dumps({"result": {"btType": "BTFSValueMap", "value": entries}}).encode()
        if method == "POST" and path.endswith("/features/updates"):
            for changed in payload["features"]:
                self.features = [changed if f["featureId"] == changed["featureId"] else f for f in self.features]
            self.edit_by_owner()
            return 200, {}, json.dumps({"featureStates": self.states, "sourceMicroversion": self.version}).encode()
        if method == "POST" and path.endswith("/features"):
            feature = dict(payload["feature"], featureId=f"Fnew{len(self.features):012d}_0")
            status = "ERROR" if "bad" in feature["name"].lower() else "OK"
            self.features.append(feature)
            self.states[feature["featureId"]] = {"featureStatus": status}
            self.edit_by_owner()
            return 200, {}, json.dumps({"feature": feature, "featureState": {"featureStatus": status},
                                        "sourceMicroversion": self.version}).encode()
        if method == "POST" and path.endswith("/featurescript"):
            entries = [{"key": {"btType": "BTFSValueString", "value": f["featureId"]},
                        "value": {"btType": "BTFSValueMap", "value": [
                            {"key": {"btType": "BTFSValueString", "value": "statusEnum"},
                             "value": {"btType": "BTFSValueString", "value": "EXTRUDE_NO_SELECTED_REGION"}}]}}
                       for f in self.features if f["featureId"] in payload["script"]]
            return 200, {}, json.dumps({"result": {"btType": "BTFSValueMap", "value": entries}}).encode()
        return 404, {}, b'{"message": "the fake does not know this call"}'


def typed(value):
    """Plain Python as Onshape's typed FeatureScript values."""
    if isinstance(value, list):
        return {"btType": "BTFSValueArray", "value": [typed(v) for v in value]}
    return {"btType": "BTFSValueString" if isinstance(value, str) else "BTFSValueNumber", "value": value}


def context(tmp, budget=20):
    """A full tool context whose CAD system is the fake. Returns (context, fake)."""
    fake = FakeOnshape()
    ledger = Ledger(Path(tmp) / "ledger.json", "demo", budget)
    backend = OnshapeBackend(OnshapeClient(Credentials("ACCESS", "SECRET"), ledger, transport=fake))
    return Context(Config({}, []), ledger, backend, LocalRunner(), Store(Path(tmp) / "store")), fake
