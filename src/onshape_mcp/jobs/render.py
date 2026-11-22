#!/usr/bin/env python3
"""Geometry job: a picture of the mesh from four sides, for a second look after the numbers.

    python render.py model.stl      one JSON object on stdout: {"png": base64, "width": ..., "height": ...}

Needs Pillow. Triangles are painted far to near, which is exact enough to judge a shape but can misdraw
where parts interleave closely. Do not measure from the picture.
"""
import base64
import io
import json
import math
import re
import struct
import sys

PANEL = (640, 480)
SCALE = 2  # drawn at twice the size, then reduced, to smooth the edges
VIEWS = [  # name, what the screen axes are, camera direction (right, up, towards the viewer)
    ("from above and the front-right", None, ((0.7071, 0.7071, 0.0), (-0.3536, 0.3536, 0.866), (0.6124, -0.6124, 0.5))),
    ("front: X to the right, Z up", None, ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))),
    ("top: X to the right, Y up", None, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))),
    ("right side: Y to the right, Z up", None, ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0))),
]
LIGHT = (0.35, -0.45, 0.82)
SHARP = math.cos(math.radians(25))


def read_triangles(data):
    if len(data) >= 84 and len(data) == 84 + 50 * struct.unpack_from("<I", data, 80)[0]:
        return [(t[3:6], t[6:9], t[9:12]) for t in struct.iter_unpack("<12fH", memoryview(data)[84:])]
    points = [tuple(float(x) for x in p) for p in re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
    if not points or len(points) % 3:
        raise ValueError("not an STL file (neither binary nor ASCII)")
    return [tuple(points[i:i + 3]) for i in range(0, len(points), 3)]


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def render(data):
    from PIL import Image, ImageDraw

    triangles = read_triangles(data)
    normals = []
    for a, b, c in triangles:
        n = ((b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
             (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2]),
             (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        size = math.sqrt(dot(n, n)) or 1.0
        normals.append((n[0] / size, n[1] / size, n[2] / size))
    # an edge is drawn where two faces meet at a clear angle, or where the mesh is open
    partner = {}
    for t, tri in enumerate(triangles):
        for i in range(3):
            key = (tri[i], tri[(i + 1) % 3])
            partner[key if key[0] < key[1] else (key[1], key[0])] = partner.get(key if key[0] < key[1] else (key[1], key[0]), []) + [t]
    sharp = {key for key, ts in partner.items() if len(ts) != 2 or dot(normals[ts[0]], normals[ts[1]]) < SHARP}
    lo = [min(p[k] for tri in triangles for p in tri) for k in range(3)]
    hi = [max(p[k] for tri in triangles for p in tri) for k in range(3)]

    width, height = PANEL[0] * 2 * SCALE, PANEL[1] * 2 * SCALE
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for index, (_, _, (right, up, toward)) in enumerate(VIEWS):
        ox, oy = (index % 2) * PANEL[0] * SCALE, (index // 2) * PANEL[1] * SCALE
        corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
        xs, ys = [dot(c, right) for c in corners], [dot(c, up) for c in corners]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
        zoom = min(PANEL[0] * SCALE * 0.86 / max(max(xs) - min(xs), 1e-9), PANEL[1] * SCALE * 0.80 / max(max(ys) - min(ys), 1e-9), 1e9)
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        del span

        def screen(p, ox=ox, oy=oy, zoom=zoom, cx=cx, cy=cy, right=right, up=up):
            return (ox + PANEL[0] * SCALE / 2 + (dot(p, right) - cx) * zoom, oy + PANEL[1] * SCALE / 2 - (dot(p, up) - cy) * zoom)

        order = sorted((t for t in range(len(triangles)) if dot(normals[t], toward) > 1e-6),
                       key=lambda t: sum(dot(p, toward) for p in triangles[t]))
        for t in order:
            tri = triangles[t]
            shade = 0.42 + 0.58 * max(0.0, dot(normals[t], LIGHT))
            colour = (int(176 * shade + 30), int(190 * shade + 30), int(212 * shade + 30))
            points = [screen(p) for p in tri]
            draw.polygon(points, fill=colour)
            for i in range(3):
                key = (tri[i], tri[(i + 1) % 3])
                if (key if key[0] < key[1] else (key[1], key[0])) in sharp:
                    draw.line([points[i], points[(i + 1) % 3]], fill=(40, 46, 58), width=SCALE)
    image = image.resize((PANEL[0] * 2, PANEL[1] * 2), Image.LANCZOS)
    draw = ImageDraw.Draw(image)
    for index, (name, _, _) in enumerate(VIEWS):
        draw.text(((index % 2) * PANEL[0] + 10, (index // 2) * PANEL[1] + 8), name, fill=(60, 60, 60))
    draw.line([(PANEL[0], 0), (PANEL[0], PANEL[1] * 2)], fill=(210, 210, 210))
    draw.line([(0, PANEL[1]), (PANEL[0] * 2, PANEL[1])], fill=(210, 210, 210))
    size = " x ".join(f"{b - a:.1f}" for a, b in zip(lo, hi))
    draw.text((10, PANEL[1] * 2 - 18), f"overall {size} (X x Y x Z), {len(triangles)} triangles", fill=(60, 60, 60))
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return {"png": base64.b64encode(out.getvalue()).decode(), "width": image.width, "height": image.height}


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("This runner's Python has no Pillow. Point [runner] python at an environment that has it.")
    with open(argv[1], "rb") as fh:
        json.dump(render(fh.read()), sys.stdout)


if __name__ == "__main__":
    main(sys.argv)
