# The goal

**The AI does about 90 % of a parametric CAD job and hands back a model the owner can open in their own
CAD tool and keep editing by hand: right the first time, proven by numbers, within the request budget.**

Everything in this project is judged against that sentence. If a feature does not serve it, it does not go in.

## What "done right" means

| # | Test | How we know |
|---|---|---|
| 1 | **A person can keep editing it.** Native features, a handful of plainly named variables, every sketch fully defined from meaningful references, named parts, joints placed on real geometry. | The owner changes a dimension in the CAD tool and the model rebuilds without errors. They never have to rebuild the AI's work. |
| 2 | **One source of truth.** The CAD document is the model. We read it and write to it; we never keep a second model that can drift away from it. | Every check runs on geometry pulled from the document, at a known version. |
| 3 | **Proven by numbers.** Fit, clearance, motion and printability are computed before anyone looks. | No tool ever answers a bare "OK". It reports numbers and the largest remaining mismatch. |
| 4 | **Frugal.** Every request to a metered CAD service is counted, against a budget only the owner can raise. Read once, cache, write in batches. | A typical change costs 8 requests or fewer. The count is shown after every call. |
| 5 | **Pluggable.** The AI client, the CAD system and the machine that does the heavy computing are each chosen in a config file. | Swapping one does not touch the other two. |

## What this is not

- Not text-to-CAD without a person. The owner stays the designer; the AI is the fast pair of hands.
- Not a wrapper around every endpoint of a CAD API. A few tools at the level of design intent.
- Not a replacement for the CAD tool's own interface. The owner's edits there are first-class.

## The failure this project exists to prevent

An AI built a parametric model from its own scripts: dozens of derived variables in a separate document,
sketches placed by computed coordinates, joints on computed positions instead of the real bearing geometry.
It looked finished and nothing about it could be adjusted by hand. The owner had to build the part again.
