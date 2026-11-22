# Building a model a person can keep editing

These rules apply to every CAD system. They exist because a model that only its author can change is a
failed model, however correct its shape is.

## Before you build

1. **Read the document first.** The owner edits it by hand. What you remember about it is out of date; what
   `cad_read` returns is the truth. Never keep your own parallel copy of the design and push it over theirs.
2. **Say the plan in one line and wait for a yes** when the change is more than a small fix:
   what you will add, to which tab, and roughly how many requests it costs.
3. **Ask for a real measurement rather than guess one.** A guessed number that looks measured is the most
   expensive kind of mistake.

## The model

4. **Start each part or sub-assembly from one layout sketch** holding the few real numbers of the design
   (overall sizes, the axis positions, the bearing circles). Everything else refers to it.
5. **Few variables, where the owner will look for them.** At most about eight, plainly named
   (`thickness`, `tube_diameter`), at the top of the same tab. Never a variable that is only arithmetic on other
   variables, and never a separate variables document unless the owner asks for one.
6. **Every sketch fully defined.** No entity may still move. Position things from the origin, from the layout
   sketch or from real edges of the part, with dimensions the owner would recognise from their own measurements.
   Do not place geometry by computed coordinates. `cad_build` refuses a sketch that can still move and lists
   what moves; add the missing dimension, do not work around the refusal.
7. **True shapes.** A circle is a circle with a diameter, not two arcs or a polygon.
8. **Native features, few of them, named for what they are** ("Motor window", not "Extrude 7").
   No helper features that exist only for the script's convenience. Round corners with a fillet feature the
   owner can change in one place, not with arcs in the sketch.
9. **Name every part.** Give `"part"` on the extrude that makes it, or a `name_part` step.
10. **Refer to existing geometry by what it is** (the face at this position, the edge of that feature), not by
    an index that changes when the model changes.

## Assemblies

11. **One fixed part.** Everything else is held by joints.
12. **Joints sit on real geometry:** the centre of the bearing circle, the pivot hole. If the owner moves the
    hole, the joint must follow without anyone retyping a number.
13. **Move it before handing it over.** Sweep each joint through its range with `cad_motion` and report the
    smallest clearance and where it occurs. Parts that touch at every position are bearings or supports; a pair
    that is clear at some angles and touching at others is a collision.

## Checking

14. **Numbers first, pictures second.** After each change report what changed: volumes, sizes, body count,
    rebuild errors.
15. **Never report a bare "done" or "OK".** Report the numbers and the largest thing that is still wrong or
    unverified, even when it is small.
16. **Check on geometry pulled from the document,** at the version you just wrote, not on what you intended
    to build.

## Models that already exist

`cad_read` says, for every sketch, whether it is fully defined and whether its dimensions have labels.

- A sketch **not fully defined** needs the owner's decision about the missing dimension; say which shape can
  still move and ask, rather than inventing a number.
- Dimensions **without labels** may show no number to click in the sketch editor. `cad_repair` adds the labels
  and changes nothing else; show the dry run and its cost first.
- A sketch **not assessed** uses something this tool does not judge (tangent, offset, another part's geometry).
  Say so; do not call it fully defined.

## Cost

17. **Read once, write in batches.** One read returns the whole tree. Send a whole sketch in one request and
    several edits in one request.
18. **Never retry a failed write blindly.** Read the error text, fix the cause, then send once.
19. **Work in small steps the owner can look at:** about ten features, then stop and show the numbers.

## A sketch for `cad_build`, as an example

A 60 x 40 plate with a 6 mm hole, the plate corner on the origin, every number one the owner would measure:

```json
{"sketch": {"name": "Bracket outline", "plane": "Top",
  "entities": [
    {"id": "plate", "rectangle": [0, 0, 60, 40]},
    {"id": "hole", "circle": [15, 20, 3]}
  ],
  "constraints": [
    ["coincident", "plate.bottom.start", "origin"],
    ["length", "plate.bottom", "60 mm"],
    ["length", "plate.right", "40 mm"],
    ["diameter", "hole", "6 mm"],
    ["distance_x", "origin", "hole.center", "15 mm"],
    ["distance_y", "origin", "hole.center", "20 mm"]
  ]}}
```

Draw the shapes where the dimensions put them. A rectangle arrives closed and square, so it needs only its
position and its two sizes. Count before sending: a circle has 3 movements (centre x, centre y, size), a line 4,
an arc 5, a point 2; every dimension or constraint removes one or two. The gate does this count exactly.
