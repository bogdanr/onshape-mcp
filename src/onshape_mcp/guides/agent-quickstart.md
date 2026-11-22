# How an AI agent uses these tools

You are the fast pair of hands. The owner is the designer, and they keep editing the same CAD document by
hand. Your work is finished when they can open the model and carry on, not when a shape looks right.

## The loop for one change

1. **`cad_status`** (0 requests). Read the budget line. If what is left will not cover the job, stop and ask
   the owner for a number.
2. **`cad_read`** the tab (1 request). The owner may have edited it since you last looked, so what you
   remember is out of date. Use `"source": "cached"` only when you know nothing changed.
3. **`cad_inspect`** each feature you are about to touch (0 requests).
4. **Dry-run the change.** `cad_set`, `cad_build` and `cad_repair` send nothing unless `dry_run` is false.
   The dry run prints the plan, the sketch gate's verdict and the cost.
5. **Show the owner** the dry run and its cost, and wait for a yes, for anything larger than a small fix.
6. **Apply** it: the same call with `"dry_run": false`.
7. **`cad_check`** (3 requests after a change). For an assembly, also `cad_motion` on each joint that moves.
   `cad_view` is for a second look at the shape; never measure from it.
8. **Report** what changed, the numbers from the check, the largest thing still wrong or unverified, and the
   requests spent and left.

A typical change (read, set, check) costs 5 requests.

## What each tool costs

| Tool | Requests to the CAD service |
|---|---|
| `cad_status` | 0 |
| `cad_read` | 1, or 0 with `"source": "cached"` |
| `cad_inspect` | 0 |
| `cad_check` | 3 after a change, 1 if the model is unchanged, 0 cached or for a file |
| `cad_view` | 0 after a check |
| `cad_motion` | 1, plus 3 for each Part Studio whose geometry is not yet saved at this version |
| `cad_set` | 0 for the dry run, 1 to apply, however many numbers change |
| `cad_build` | 0 for the dry run, 1 per feature, plus 2 if parts are named |
| `cad_repair` | 0 for the dry run, 1 per sketch |

Every answer ends with the requests it cost and what is left. Failed requests are counted too.

## What to pass

- **`model`** is the link to the tab, exactly as copied from the browser's address bar: a Part Studio tab for
  most tools, the assembly tab for `cad_motion`. A path to an `.stl` file also works for `cad_check` and
  `cad_view`, and costs nothing.
- **Values carry their unit:** `"6 mm"`, `"30 deg"`, `"#thickness / 2"`. A bare number is refused.
- **`number` in `cad_set`** is the label exactly as `cad_read` printed it, for example `depth`,
  `diameter(circle 3)` or `#thickness`.
- The full step and sketch format is in the description of `cad_build`. The modelling rules, with a worked
  sketch, are in the guide `editable-models`. Read it before the first `cad_build` or `cad_set`.

## Rules that are never broken

- **The budget is the owner's.** Never edit the config file to raise it. Never reach the CAD service another
  way (your own script, `curl`) to get around it. A yes to a plan is not a yes to more requests.
- **Write only to the tab the owner named** for this job.
- **The document is the model.** Never build the design in a script of your own and push it over theirs.
- **Never work around the sketch gate.** If it lists a movement, add the dimension a person would add.
- **Never retry a failed write unchanged.** Read the error, fix the cause, send once.
- **Never answer a bare "done" or "OK".** Give the numbers and the largest thing still unverified.
- **Never guess a measurement.** Ask the owner for it.

## When a tool refuses or fails

| The answer says | What happened | What to do |
|---|---|---|
| `has used N of its N approved requests. Nothing was sent.` | The budget is spent. | Stop. Tell the owner what is done, what is left and how many requests it needs. |
| `The yearly total is ...` | The account's yearly allowance is nearly used. | Stop and ask the owner. |
| `The document changed after it was read, so nothing was changed.` | The owner edited the document after your `cad_read`. | `cad_read` again (1 request), then redo the dry run on the new tree. |
| `NOT fully defined: N movements still free` | The gate found geometry that can still move, and lists it. | Add a dimension or constraint for each movement, from the origin or a real shape. Dry-run again; it is free. |
| `repeated` constraints listed | A constraint adds nothing the others do not already fix. | Remove it. |
| `Nothing was sent. Fix these first:` | One or more steps of a `cad_build` are wrong; each is listed. | Fix every listed step, then dry-run again. |
| `added, but it does not rebuild` | The CAD system accepted a feature and could not build it. Later steps were not started. | Read the error text. The failed feature is in the document: tell the owner, then `cad_read` before anything else. |
| a sketch is `not assessed` | It uses something this tool does not judge (tangent, offset, another part's geometry). | Say so. Do not call it fully defined. |
| `Cannot reach ... over SSH` | The machine that runs the geometry jobs is off or unreachable. | Tell the owner. Reading still works; checking, pictures and motion wait. |
| `... cannot ...` from a backend | This CAD system, or a file, does not support that action. | Say so. Do not imitate it another way. |

## From a shell instead of MCP

The same tools are commands; the names lose their `cad_` prefix and lists are JSON text:

```sh
onshape-mcp guide                        # these pages; costs nothing
onshape-mcp status
onshape-mcp read  "<link>"
onshape-mcp set   "<link>" '[{"feature": "Plate", "number": "depth", "to": "6 mm"}]'   # a dry run
onshape-mcp set   "<link>" '[{"feature": "Plate", "number": "depth", "to": "6 mm"}]' --no-dry-run
onshape-mcp check "<link>"
```

Run them in the design project's folder, where its `onshape-mcp.toml` is. A command that fails exits with
status 1 and says why on standard error.
