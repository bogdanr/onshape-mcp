#!/usr/bin/env python3
"""Geometry job: what is in this mesh, in numbers. Standard library only, so it runs on any runner.

    python mesh_report.py model.stl     one JSON object on stdout
    python mesh_report.py --selftest    the same for a built-in 10 x 10 x 10 cube (proves a runner works)

Bodies are found by connectivity through shared edges, so parts that merely touch stay separate. Lengths are in
the STL's own unit. A tessellated curved body reads slightly smaller than the true solid.

Round features are full cylinders found in the mesh: holes and bosses, each with its diameter, axis, position
and length. Cones, fillets and partial arcs are not listed.
"""
import json
import math
import re
import struct
import sys

ROUND_TOLERANCE = 0.005  # how far a vertex may be from the fitted circle, in the mesh's unit
MAX_ROUND = 60  # per body; a perforated sheet should not flood the report


def read_triangles(data):
    """Yield three (x, y, z) tuples per triangle, from a binary or an ASCII STL."""
    if len(data) >= 84 and len(data) == 84 + 50 * struct.unpack_from("<I", data, 80)[0]:
        for t in struct.iter_unpack("<12fH", memoryview(data)[84:]):
            yield t[3:6], t[6:9], t[9:12]
        return
    found = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
    if not found or len(found) % 3:
        raise ValueError("not an STL file (neither binary nor ASCII)")
    points = [tuple(float(x) for x in p) for p in found]
    for i in range(0, len(points), 3):
        yield points[i], points[i + 1], points[i + 2]


def report(data):
    index, coords, faces = {}, [], []
    degenerate = 0

    def vertex(p):
        i = index.get(p)
        if i is None:
            i = index[p] = len(coords)
            coords.append(p)
        return i

    for p0, p1, p2 in read_triangles(data):
        a, b, c = vertex(p0), vertex(p1), vertex(p2)
        if a == b or b == c or a == c:
            degenerate += 1
            continue
        faces.append((a, b, c))

    # Two triangles are one body when they share an edge no other triangle uses. Parts that merely touch share
    # corners, or an edge used by four triangles, and stay separate bodies.
    users = {}
    for t, (a, b, c) in enumerate(faces):
        for edge in ((a, b), (b, c), (c, a)):
            users.setdefault((min(edge), max(edge)), []).append(t)
    parent = list(range(len(faces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for pair in users.values():
        if len(pair) == 2:
            parent[find(pair[0])] = find(pair[1])

    bodies = {}
    for t, (a, b, c) in enumerate(faces):
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = coords[a], coords[b], coords[c]
        body = bodies.setdefault(find(t), {"six": 0.0, "lo": [x0, y0, z0], "hi": [x0, y0, z0], "faces": [], "edges": {}})
        body["six"] += x0 * (y1 * z2 - z1 * y2) - y0 * (x1 * z2 - z1 * x2) + z0 * (x1 * y2 - y1 * x2)
        body["faces"].append((a, b, c))
        lo, hi = body["lo"], body["hi"]
        for p in (coords[a], coords[b], coords[c]):
            for k in range(3):
                if p[k] < lo[k]:
                    lo[k] = p[k]
                elif p[k] > hi[k]:
                    hi[k] = p[k]
        for edge in ((a, b), (b, c), (c, a)):
            body["edges"][edge] = body["edges"].get(edge, 0) + 1

    out = [{
        "volume": round(body["six"] / 6, 4),
        "bbox_min": [round(v, 4) for v in body["lo"]],
        "bbox_max": [round(v, 4) for v in body["hi"]],
        "triangles": len(body["faces"]),
        # closed and consistently oriented: within the body, every edge is used once each way
        "watertight": all(n == 1 and body["edges"].get((b, a)) == 1 for (a, b), n in body["edges"].items()),
        "round": round_features(coords, body["faces"]),
    } for body in bodies.values()]
    out.sort(key=lambda b: -b["volume"])
    return {"bodies": out, "triangles": len(faces), "degenerate_triangles": degenerate}


def _unit(v):
    size = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / size, v[1] / size, v[2] / size) if size > 1e-12 else None


def _fit_circle(points):
    """Least-squares circle through 2D points: (centre x, centre y, radius, worst distance from it)."""
    n = len(points)
    mx, my = sum(p[0] for p in points) / n, sum(p[1] for p in points) / n
    suu = svv = suv = suuu = svvv = suvv = svuu = 0.0
    for x, y in points:
        u, v = x - mx, y - my
        suu += u * u
        svv += v * v
        suv += u * v
        suuu += u * u * u
        svvv += v * v * v
        suvv += u * v * v
        svuu += v * u * u
    det = suu * svv - suv * suv
    if abs(det) < 1e-12 * max(suu, svv, 1e-12) ** 2:
        return None  # the points are on a line: a flat wall, not a cylinder
    uc = (0.5 * (suuu + suvv) * svv - 0.5 * (svvv + svuu) * suv) / det
    vc = (0.5 * (svvv + svuu) * suu - 0.5 * (suuu + suvv) * suv) / det
    radius = math.sqrt(uc * uc + vc * vc + (suu + svv) / n)
    cx, cy = mx + uc, my + vc
    return cx, cy, radius, max(abs(math.hypot(x - cx, y - cy) - radius) for x, y in points)


def round_features(coords, faces):
    """Full cylinders in one body: [{"kind": "hole"|"boss", "diameter", "axis", "center", "length"}]."""
    normals = []
    weight = {}
    for a, b, c in faces:
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = coords[a], coords[b], coords[c]
        cross = ((y1 - y0) * (z2 - z0) - (z1 - z0) * (y2 - y0), (z1 - z0) * (x2 - x0) - (x1 - x0) * (z2 - z0),
                 (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0))
        n = _unit(cross)
        normals.append(n)
        if n:
            key = tuple(round(v, 3) for v in n)
            key = key if key > tuple(-v for v in key) else tuple(-v for v in key)
            weight[key] = weight.get(key, 0.0) + math.sqrt(cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2)
    # axes worth trying: the model axes, and the normals of the largest flat faces (holes are drilled square to faces)
    directions = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    for key in sorted(weight, key=weight.get, reverse=True)[:6]:
        d = _unit(key)
        if d and all(abs(d[0] * e[0] + d[1] * e[1] + d[2] * e[2]) < 0.999 for e in directions):
            directions.append(d)
    neighbours = {}
    for t, (a, b, c) in enumerate(faces):
        for edge in ((a, b), (b, c), (c, a)):
            neighbours.setdefault((min(edge), max(edge)), []).append(t)

    found = {}
    for d in directions:
        u = _unit((d[1], -d[0], 0.0)) or (1.0, 0.0, 0.0)
        v = (d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0])
        wall = [n is not None and abs(n[0] * d[0] + n[1] * d[1] + n[2] * d[2]) < 0.01 for n in normals]
        parent = list(range(len(faces)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for pair in neighbours.values():  # join wall triangles that meet smoothly (not across a sharp corner)
            if len(pair) == 2 and wall[pair[0]] and wall[pair[1]]:
                na, nb = normals[pair[0]], normals[pair[1]]
                if na[0] * nb[0] + na[1] * nb[1] + na[2] * nb[2] > 0.766:
                    parent[find(pair[0])] = find(pair[1])
        groups = {}
        for t in range(len(faces)):
            if wall[t]:
                groups.setdefault(find(t), []).append(t)
        for members in groups.values():
            if len(members) < 8:
                continue
            ids = {i for t in members for i in faces[t]}
            flat = [(coords[i][0] * u[0] + coords[i][1] * u[1] + coords[i][2] * u[2],
                     coords[i][0] * v[0] + coords[i][1] * v[1] + coords[i][2] * v[2]) for i in ids]
            fit = _fit_circle(flat)
            if fit is None or fit[3] > ROUND_TOLERANCE or fit[2] > 1e5:
                continue
            cx, cy, radius, _ = fit
            along = [coords[i][0] * d[0] + coords[i][1] * d[1] + coords[i][2] * d[2] for i in ids]
            t0 = members[0]
            mid = [sum(coords[i][k] for i in faces[t0]) / 3 for k in range(3)]
            out_x = mid[0] * u[0] + mid[1] * u[1] + mid[2] * u[2] - cx
            out_y = mid[0] * v[0] + mid[1] * v[1] + mid[2] * v[2] - cy
            n0 = normals[t0]
            outward = out_x * (n0[0] * u[0] + n0[1] * u[1] + n0[2] * u[2]) + out_y * (n0[0] * v[0] + n0[1] * v[1] + n0[2] * v[2])
            key = (directions.index(d), "boss" if outward > 0 else "hole", round(cx, 2), round(cy, 2), round(radius, 2))
            entry = found.setdefault(key, {"d": d, "u": u, "v": v, "c": (cx, cy), "r": radius, "lo": min(along), "hi": max(along), "arc": set()})
            entry["lo"], entry["hi"] = min(entry["lo"], min(along)), max(entry["hi"], max(along))
            entry["arc"].update(int(math.degrees(math.atan2(y - cy, x - cx)) % 360 // 10) for x, y in flat)

    out = []
    for (_, kind, _, _, _), e in found.items():
        if len(e["arc"]) < 35:  # not all the way round: a fillet or a notch, not a hole or a boss
            continue
        middle = (e["lo"] + e["hi"]) / 2
        centre = [e["c"][0] * e["u"][k] + e["c"][1] * e["v"][k] + middle * e["d"][k] for k in range(3)]
        out.append({"kind": kind, "diameter": round(2 * e["r"], 3), "axis": [round(x, 4) for x in e["d"]],
                    "center": [round(x, 3) for x in centre], "length": round(e["hi"] - e["lo"], 3)})
    out.sort(key=lambda f: (-f["diameter"], f["kind"], f["center"]))
    return out[:MAX_ROUND]


def cube_stl(size=10.0):
    """A closed cube as binary STL, for self-tests."""
    s = size
    corners = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    body = b"".join(struct.pack("<12fH", 0, 0, 0, *corners[a], *corners[b], *corners[c], 0) for a, b, c in faces)
    return b"\0" * 80 + struct.pack("<I", len(faces)) + body


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    if argv[1] == "--selftest":
        answer = report(cube_stl())
        answer["python"] = sys.version.split()[0]
        answer["modules"] = {}
        for name in ("numpy", "scipy", "PIL"):  # what the heavier jobs need
            try:
                answer["modules"][name] = __import__(name).__version__
            except Exception:
                answer["modules"][name] = None
    else:
        with open(argv[1], "rb") as fh:
            answer = report(fh.read())
    json.dump(answer, sys.stdout)


if __name__ == "__main__":
    main(sys.argv)
