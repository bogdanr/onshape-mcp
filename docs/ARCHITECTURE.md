# Architecture

Read [GOAL.md](../GOAL.md) first. This page says how the code is arranged to serve it, and what comes next.

## The shape of it

```
  AI client                         a person, or any agent with a shell
  (Claude Code, Claude Desktop,
   any MCP client)
        |  MCP over stdio                    |  command line
        v                                    v
  mcp_server.py ------------------------- cli.py          the AI plug: two thin adapters
                        |
                    tools.py                              a few tools at the level of design intent,
                        |                                 defined once, text answers made of numbers
        +---------------+----------------+
        |               |                |
   CadBackend        Runner            Store + Ledger
   backends/         runners/          store.py, budget.py
   (onshape)         (local, ssh)
        |               |
   the CAD system    jobs/*.py                            plain scripts: files in, one JSON object out
   (source of truth) on this or another machine
```

| Module | Job | Rule it enforces |
|---|---|---|
| `tools.py` | The tools and their registry. The MCP server and the CLI are generated from the same definitions. | No tool answers a bare "OK"; every answer ends with the requests it cost; writing is a dry run unless asked otherwise. |
| `sketch.py` | A sketch in neutral terms, and the gate: solve it, count what can still move, name what is repeated. | No sketch reaches a CAD system unless it is fully defined. |
| `motion.py` | An assembly as rigid groups joined by joints, and which side moves when one joint turns. | A joint is judged against the parts' real geometry, not against its own numbers. |
| `expr.py` | Reads values such as `#thickness / 2 + 3 mm` without running code. | A length without a unit is refused. |
| `backends/base.py` | The CAD plug. Reading is required (`read_studio`, `outline`, `describe_feature`, `export_mesh`); writing (`plan_changes`, `apply_changes`, `add`, `name_parts`, `plan_repair`) and assemblies (`read_assembly`, `assembly`, `part_boxes`) are optional and answer "this backend cannot" by default. | Tools never talk to a CAD system any other way. The interface grows only when a tool needs it. |
| `backends/onshape/` | Onshape: `client.py` (REST), `backend.py`, `outline.py` (feature JSON to readable text), `build.py` (features written the way the editor writes them), `sketch_io.py` (existing sketches read back for judging and re-solving). | Every request counted, each redirect hop too; no automatic retry; Onshape's error text is passed on; the keys go to Onshape's hosts only; every write is pinned to the version last seen and refused if the document moved on. |
| `backends/files.py` | Mesh files on disk as a second, read-only backend. | The version of a file is its content, so nothing is measured twice. |
| `runners/` | The compute plug: run a job locally or over SSH. | A job is a file you can also run by hand. Nothing is installed on the far machine. |
| `jobs/` | Geometry jobs: `mesh_report.py` (bodies, volumes, holes and bosses; standard library only), `render.py` (four views; needs Pillow), `sweep.py` (clearances through a joint's range; needs numpy and scipy). | Checks run on geometry pulled from the CAD document, at one exact version. |
| `store.py` | Snapshots by document version: feature tree, mesh, part lists, reports, pictures. | Reading the same version twice costs nothing. Reports are compared across versions to say what changed. |
| `budget.py` | The request ledger. | Count before sending. The budget comes from the config; no tool can raise it. |
| `config.py` | TOML settings, merged from a machine-wide file, the design project's file and `$ONSHAPE_MCP_CONFIG`. | The default budget is 0. |
| `guides/` | What an AI agent is told: the quick start (the loop, the costs, every refusal) and the modelling rules. Served as MCP resources and printed by `onshape-mcp guide`. | The rules live in one place, for every AI client. |

## Adding a plug

- **A CAD system:** implement `CadBackend` in `backends/<name>/`, add it to `backends/__init__.py`. Start with
  the four reading methods, as `backends/files.py` does; the tools say "this backend cannot" for the rest. If
  the system is not metered, ignore the ledger.
- **A runner:** implement `Runner.run(script, inputs, args) -> dict` in `runners/<name>.py`, add it to
  `runners/__init__.py`.
- **An AI client that does not speak MCP:** use the CLI, or call `tools.TOOLS` from Python.

## Roadmap

Ordered by what the goal needs most. Each step ended with a live test on a real model and a counted request cost.

| Step | Adds | Status |
|---|---|---|
| 0.1 | Skeleton; read, inspect, check; ledger; store; local and SSH runners; MCP and CLI | done, live |
| 0.2 | The sketch gate. `cad_build` for variables, sketches and extrudes. `cad_set`, refused if the document changed. Onshape's own error text when a feature fails. Dry run by default. | done, live |
| 0.3 | Offset planes, fillets, chamfers, part names. `cad_repair` for dimensions without labels. Existing sketches read back and judged; a changed sketch re-solved before sending. | done, live |
| 0.4 | `cad_motion`: joints, whether each sits on real geometry, clearances through a range of angles. | done, live on a 67-part assembly |
| 0.5 | Holes and bosses in `cad_check`. `cad_view`. The file backend as a second CAD plug. Server checked with the reference MCP client. | done |
| later | Tangent and angle in sketches; sketches on faces; revolve; building assemblies and their joints; sub-assemblies and slider joints; STEP files; a second full CAD system and the rename. | open |

## What live testing taught us about Onshape

Each of these was observed on a real document, not read in documentation.

- **A mesh export answers with a redirect to another Onshape host** (a regional server). The keys must be sent
  there too, and the hop is a second request. So a fresh check costs 3 requests: the read, the export, the hop.
- **An export can be pinned to the exact microversion the feature tree was read at,** so the mesh and the tree
  always describe the same model. An assembly names the microversion of every part it uses.
- **Every write can be pinned to the version last seen** (`sourceMicroversion` with `rejectMicroversionSkew`),
  for adding a feature, updating one, and the batch update alike. Each answer gives the new version to pin the
  next write to. The owner's later edits cannot be overwritten by an out-of-date change.
- **The batch update reports status only for the features it changed.** Features that depend on them are
  reported by the next read.
- **After a dimension is changed through the API, the solid is rebuilt correctly but the coordinates stored
  in the sketch stay as sent.** So changed sketches are solved here first and sent with exact coordinates.
- **A whole-feature update adds parameters to an existing sketch,** which is how dimension labels are repaired.
- **Every dimension drawn in the sketch editor carries label parameters; dimensions sent without them are
  accepted and do constrain the sketch.** Another project's tested notes say the editor then shows no number
  for them. We have not yet confirmed that by eye. `cad_build` always writes the labels, `cad_read` flags
  sketches without them, and `cad_repair` adds them.
- **A FeatureScript evaluation that fails returns no result and no HTTP error.** Ours once failed on a variable
  named `box`, a reserved word, and came back looking like "no parts". An empty evaluation is now an error.
- **Parts that touch share corner points and edges in the exported mesh,** so "connected triangles" is not
  "one part". Bodies are separated through edges that exactly two triangles share.
- **A tessellated mesh measures a plate with holes to within 0.01 % of the exact volume,** and a part with
  many curved faces to within 0.15 %.

## What we learned from other projects

Before starting we read the source of seven open-source CAD servers for AI clients. What we kept:

- **A few tools, not a hundred.** Projects with 50 to 175 thin tools cost the model its context and teach it
  nothing about design intent.
- **Numbers after every step,** and errors that say where. One project measured that a plain "conforms"
  verdict made agents stop early, so we never give one.
- **Fully defined sketches are the gap.** Every sketch helper we read leaves geometry free to move, places it
  by coordinates, or draws circles as arcs.
- **Tests against real responses.** One project had all of its model-building tools rejected by the CAD
  service for months while hundreds of simulated-response tests passed.
- **Knowledge offline.** API lookup and tested recipes that cost no requests.
- **Atomic, labelled changes** and rebuild errors reported as what is new, fixed and still broken.
- **Nobody counts requests, checks motion, or aims at a model a person keeps editing.** That is this project.
