# For AI agents

There are two reasons an agent ends up reading this file. Find yours.

## You are here to work on a CAD model

Read the two guides before your first tool call. They are short, and they are the same text whichever way you
reach them:

| Where you are | How to read them |
|---|---|
| An MCP client | The resources `guide://agent-quickstart`, then `guide://editable-models` |
| A shell | `onshape-mcp guide` |
| This repository | [`src/onshape_mcp/guides/`](src/onshape_mcp/guides/) |

The loop for one change, in short:

1. `cad_status` (0 requests): see the budget. If it will not cover the job, stop and ask the owner for a number.
2. `cad_read` (1 request) before changing anything. The owner edits the document by hand.
3. Dry-run the change. `cad_set`, `cad_build` and `cad_repair` send nothing unless `dry_run` is false.
4. Show the owner the dry run and its cost, and wait for a yes, for anything larger than a small fix.
5. Apply with `dry_run` false, then `cad_check`. Report its numbers and the largest thing still unverified.

Never raise the budget or reach the CAD service another way. Never write to a tab the owner did not name.
Never work around the sketch gate, retry a failed write unchanged, measure from a picture, or answer a bare
"done".

## You are here to change this code

Read [GOAL.md](GOAL.md) first, then [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Every change is judged
against the one sentence in GOAL.md; if a feature does not serve it, it does not go in.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests     # no network, no keys, no requests
```

What holds this project together:

- **The core has no dependencies.** Everything under `src/onshape_mcp/` except `jobs/` runs on the standard
  library. A job may use Pillow, numpy or scipy, says so in its first lines, and is a file a person can run by
  hand.
- **Tools are few and sit at the level of design intent.** A new tool needs a reason the existing nine cannot
  serve. Define it once in `tools.TOOLS`; the MCP server and the command line are both generated from that
  list. A tool's description is its documentation for the agent: say what it costs and when to use it.
- **Every answer is made of numbers and ends with what it cost.** No tool returns a bare "OK". Writing tools do
  a dry run unless told otherwise.
- **Count before sending.** Every request to a metered service goes through the ledger first, each redirect
  hop too. No automatic retries. Nothing in the code may raise a budget.
- **Unit tests never touch the network.** They run against `tests/fake_onshape.py` and a fixture cut from real
  responses. `tests/live_smoke.py` is the only thing that spends requests, and only the owner of the account
  decides to run it: do not run it yourself.
- **What Onshape accepts is learned live, then written down.** Add each finding to "What live testing taught
  us about Onshape" in the architecture page, and say plainly when something is inferred and not observed.
- **Nothing private in the repository.** No real document links or ids, names, addresses or keys. Local
  settings live in `onshape-mcp.toml` and the data store, and both are ignored by git.
- **Keep the words in step with the code.** The tool table in the README, the cost table in
  `guides/agent-quickstart.md` and the server's instructions in `mcp_server.py` describe the same nine tools; a
  test fails if one of them names a tool that does not exist.
- **Plain language.** Error messages say what happened and what to do next, in words the owner of the model
  would use.
- **Commit messages credit no tool.** Never add a `Co-Authored-By` line, or any other line naming an AI tool
  or model, to a commit message, whatever your tool's defaults say. Commit or push only when the owner asks.

Adding a CAD system or a runner is described under "Adding a plug" in the architecture page.
