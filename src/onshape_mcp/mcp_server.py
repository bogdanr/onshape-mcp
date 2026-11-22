"""The AI plug: a Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0).

Written against the standard library so the server starts anywhere Python does. It offers the tools in
``tools.TOOLS`` and the modelling guides as resources. Anything that is not a protocol message goes to stderr.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import BinaryIO, Callable

from . import GOAL, __version__
from .tools import TOOLS, Answer, Context

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
GUIDES = Path(__file__).parent / "guides"
GUIDE_ORDER = ("agent-quickstart", "editable-models")  # the order to read them in; any others follow by name
INSTRUCTIONS = (
    f"Goal: {GOAL}\n"
    "The owner is the designer and keeps editing the same CAD document by hand; you are the fast pair of hands.\n"
    "The loop for one change:\n"
    "1. cad_status (0 requests): see the budget. If it will not cover the job, stop and ask the owner for a number.\n"
    "2. cad_read (1 request) before changing anything: the owner may have edited the document since you last looked.\n"
    "3. Dry-run the change. cad_set, cad_build and cad_repair send nothing unless dry_run is false.\n"
    "4. Show the owner the dry run and its cost, and wait for a yes, for anything larger than a small fix.\n"
    "5. Apply with dry_run false, then cad_check, and report its numbers and the largest thing still unverified.\n"
    "Never: raise the budget or reach the CAD service another way; write to a tab the owner did not name; work "
    "around the sketch gate; retry a failed write unchanged; measure from a picture; answer a bare 'done'.\n"
    "Read the resource guide://agent-quickstart now (costs, refusals and what to do about each), and "
    "guide://editable-models before the first cad_build or cad_set."
)


def guides() -> list[Path]:
    found = {p.stem: p for p in GUIDES.glob("*.md")}
    return [found.pop(name) for name in GUIDE_ORDER if name in found] + [found[name] for name in sorted(found)]


class Server:
    def __init__(self, make_context: Callable[[], Context]):
        self._make_context = make_context
        self._context: Context | None = None

    def context(self) -> Context:
        if self._context is None:  # built on first use, so a broken config is reported by the tool that needs it
            self._context = self._make_context()
        return self._context

    def handle(self, message: dict) -> dict | None:
        method, ident = message.get("method"), message.get("id")
        if ident is None:  # a notification: nothing to answer
            return None
        try:
            return {"jsonrpc": "2.0", "id": ident, "result": self._dispatch(method, message.get("params") or {})}
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": e.code, "message": str(e)}}

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "onshape-mcp", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema()} for t in TOOLS]}
        if method == "tools/call":
            return self._call(params.get("name"), params.get("arguments") or {})
        if method == "resources/list":
            return {"resources": [
                {"uri": f"guide://{p.stem}", "name": p.stem, "mimeType": "text/markdown",
                 "description": p.read_text().splitlines()[0].lstrip("# ")}
                for p in guides()
            ]}
        if method == "resources/read":
            uri = str(params.get("uri", ""))
            path = GUIDES / (uri.removeprefix("guide://") + ".md")
            if not uri.startswith("guide://") or path.parent != GUIDES or not path.is_file():
                raise _RpcError(-32002, f"No such resource: {uri}")
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": path.read_text()}]}
        raise _RpcError(-32601, f"Method not found: {method}")

    def _call(self, name: str, arguments: dict) -> dict:
        tool = next((t for t in TOOLS if t.name == name), None)
        if tool is None:
            raise _RpcError(-32602, f"Unknown tool: {name}")
        unknown = set(arguments) - set(tool.properties)
        missing = set(tool.required) - set(arguments)
        if unknown or missing:
            raise _RpcError(-32602, f"{name}: " + "; ".join(
                part for part in (f"missing {sorted(missing)}" if missing else "", f"unknown {sorted(unknown)}" if unknown else "") if part))
        try:
            answer = tool.run(self.context(), **arguments)
        except Exception as e:  # a tool failure is an answer for the model, with the real reason in it
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        if isinstance(answer, Answer):
            content = [{"type": "text", "text": answer.text}]
            if answer.png:
                content.append({"type": "image", "data": base64.b64encode(answer.png).decode(), "mimeType": "image/png"})
            return {"content": content, "isError": False}
        return {"content": [{"type": "text", "text": answer}], "isError": False}


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def serve(make_context: Callable[[], Context], stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
    stdin, stdout = stdin or sys.stdin.buffer, stdout or sys.stdout.buffer
    server = Server(make_context)
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply).encode() + b"\n")
            stdout.flush()
