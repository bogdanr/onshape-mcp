"""The same tools from a shell, for a person or for any AI agent that can run commands.

    onshape-mcp status [--probe-runner]
    onshape-mcp read URL [--source cached]
    onshape-mcp inspect URL FEATURE
    onshape-mcp check URL [--source cached]
    onshape-mcp view URL
    onshape-mcp motion ASSEMBLY_URL [--joint NAME --from-deg -50 --to-deg 55 --step-deg 5]
    onshape-mcp repair URL [--no-dry-run]
    onshape-mcp set URL '[{"feature": "Plate", "number": "depth", "to": "6 mm"}]' [--no-dry-run]
    onshape-mcp build URL '[{"sketch": {...}}, {"extrude": {...}}]' [--no-dry-run]
    onshape-mcp serve                      the MCP server on stdin/stdout
    onshape-mcp guide [NAME]               the working rules for an AI agent; costs nothing and needs no settings
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, config
from .tools import TOOLS, Answer, Context


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onshape-mcp", description="Parametric CAD with an AI, kept editable by a person.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the MCP server on stdin/stdout.")
    guide = commands.add_parser("guide", help="Print the working rules for an AI agent: how to use the tools, then how to "
                                              "build a model a person can keep editing. Costs nothing.")
    guide.add_argument("name", nargs="?", help="One guide by name. Without it, every guide is printed in reading order.")
    for tool in TOOLS:  # one subcommand per tool, built from the same definition the MCP server offers
        sub = commands.add_parser(tool.name.removeprefix("cad_"), help=tool.description)
        for name, spec in tool.properties.items():
            if spec.get("type") == "array":
                sub.add_argument(name, type=json.loads, help="A JSON list. " + (spec.get("description") or "See the tool's description."))
            elif name in tool.required:
                sub.add_argument(name, help=spec.get("description"))
            elif spec.get("type") == "boolean" and spec.get("default") is True:
                sub.add_argument("--no-" + name.replace("_", "-"), dest=name, action="store_false", help=spec.get("description"))
            elif spec.get("type") == "boolean":
                sub.add_argument("--" + name.replace("_", "-"), dest=name, action="store_true", help=spec.get("description"))
            elif spec.get("type") == "number":
                sub.add_argument("--" + name.replace("_", "-"), dest=name, type=float, default=spec.get("default"))
            else:
                sub.add_argument("--" + name.replace("_", "-"), dest=name, default=spec.get("default"),
                                 choices=spec.get("enum"), help=spec.get("description"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = vars(_parser().parse_args(argv))
    command = args.pop("command")
    if command == "serve":
        from .mcp_server import serve

        serve(lambda: Context.create(config.load()))
        return 0
    if command == "guide":
        from .mcp_server import INSTRUCTIONS, guides

        pages = {p.stem: p for p in guides()}
        if args["name"] and args["name"] not in pages:
            print(f"No guide called \"{args['name']}\". There are: {', '.join(pages)}.", file=sys.stderr)
            return 1
        chosen = [pages[args["name"]]] if args["name"] else list(pages.values())
        print("\n\n".join(([] if args["name"] else [INSTRUCTIONS]) + [p.read_text().rstrip() for p in chosen]))
        return 0
    tool = next(t for t in TOOLS if t.name == "cad_" + command)
    try:
        answer = tool.run(Context.create(config.load()), **args)
        print(answer.text if isinstance(answer, Answer) else answer)  # a picture is saved to a file the text names
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0
