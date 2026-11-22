#!/usr/bin/env python3
"""Geometry job: turn one side of a joint through its range and measure how close the parts come.

    python sweep.py scene.json studio1.stl studio2.stl ...     one JSON object on stdout

Needs numpy and scipy. scene.json names, for each STL in order, the parts in it (id, bounding box in the studio)
and where each instance of them sits in the assembly, which instances move, the axis, and the angles.
Surfaces are sampled as points; the closest pairs are then re-measured on a much finer sampling.
"""
import json
import sys
import time

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

BOX_TOLERANCE = 0.6  # mm: a tessellated body may sit this far inside its exact bounding box, never outside it
REFINE_PAIRS = 8
RANGE = 60.0  # mm: parts further apart than this are not worth measuring


def read_stl(path):
    with open(path, "rb") as fh:
        data = fh.read()
    count = int.from_bytes(data[80:84], "little")
    if len(data) != 84 + 50 * count:
        sys.exit(f"{path}: a binary STL is expected")
    record = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("pad", "<u2")])
    return np.frombuffer(data, dtype=record, count=count, offset=84)["v"].astype(np.float64)


def components(triangles):
    """A body number for every triangle.

    Two triangles belong to one body when they share an edge that no other triangle uses. Parts that merely
    touch share corners, or an edge used by four triangles, and stay apart.
    """
    _, inverse = np.unique(triangles.reshape(-1, 3), axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    count = len(faces)
    ends = np.sort(np.stack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=1).reshape(-1, 2), axis=1)
    owner = np.repeat(np.arange(count), 3)
    key = ends[:, 0].astype(np.int64) * (int(faces.max()) + 1) + ends[:, 1]
    order = np.argsort(key, kind="stable")
    key, owner = key[order], owner[order]
    start = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    size = np.diff(np.r_[start, len(key)])
    pair = start[size == 2]
    graph = coo_matrix((np.ones(len(pair), dtype=np.int8), (owner[pair], owner[pair + 1])), shape=(count, count))
    _, label = connected_components(graph, directed=False)
    return label


def assign(triangles, parts):
    """Which part each body belongs to: the smallest part whose box holds the body. -1 where none does."""
    body = components(triangles)
    owner = np.full(len(triangles), -1)
    los = np.array([p["lo"] for p in parts])
    his = np.array([p["hi"] for p in parts])
    volumes = np.prod(his - los, axis=1)
    lost = 0
    for b in np.unique(body):
        mask = body == b
        points = triangles[mask].reshape(-1, 3)
        lo, hi = points.min(axis=0), points.max(axis=0)
        inside = np.all(los - BOX_TOLERANCE <= lo, axis=1) & np.all(hi <= his + BOX_TOLERANCE, axis=1)
        if inside.any():
            owner[mask] = np.flatnonzero(inside)[np.argmin(volumes[inside])]
        else:
            lost += 1
    return owner, lost


def sample(triangles, spacing, rng):
    """Points spread over the triangles about every ``spacing``, plus every corner. Returns (points, triangle index)."""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    index = np.repeat(np.arange(len(triangles)), np.ceil(2.0 * area / (spacing * spacing)).astype(int))
    r1, r2 = np.sqrt(rng.random(len(index))), rng.random(len(index))
    inside = (1 - r1)[:, None] * a[index] + (r1 * (1 - r2))[:, None] * b[index] + (r1 * r2)[:, None] * c[index]
    return np.vstack([inside, triangles.reshape(-1, 3)]), np.concatenate([index, np.repeat(np.arange(len(triangles)), 3)])


def rotation(axis, degrees):
    x, y, z = axis
    c, s = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
    return np.array([[c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
                     [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
                     [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)]])


def coaxial(points, origin, axis):
    """Round features of a part that are centred on the axis: [(diameter, degrees of arc it covers)], widest arc first."""
    rel = points - origin
    along = rel @ axis
    radial = rel - np.outer(along, axis)
    radius = np.round(np.linalg.norm(radial, axis=1), 2)
    values, counts = np.unique(radius, return_counts=True)
    reference = np.cross(axis, [1.0, 0.0, 0.0] if abs(axis[0]) < 0.9 else [0.0, 1.0, 0.0])
    reference /= np.linalg.norm(reference)
    other = np.cross(axis, reference)
    found = []
    for value in values[(counts >= 24) & (values > 0.05)]:
        ring = radial[radius == value]
        bins = np.unique((np.degrees(np.arctan2(ring @ other, ring @ reference)) % 360 // 10).astype(int))
        if len(bins) >= 12:  # at least a third of a turn: a hole, a boss, or a cradle
            found.append((round(2 * float(value), 2), 360 if len(bins) >= 34 else 10 * len(bins)))
    return sorted(found, key=lambda f: (-f[1], -f[0]))[:4]


def main(argv):
    scene = json.load(open(argv[1]))
    rng = np.random.default_rng(1)
    clock, spent = time.monotonic(), {}

    def lap(name):
        nonlocal clock
        spent[name] = round(time.monotonic() - clock, 1)
        clock = time.monotonic()
    origin, axis = np.array(scene["axis"]["origin"], float), np.array(scene["axis"]["direction"], float)
    axis /= np.linalg.norm(axis)
    spacing = float(scene["spacing"])

    names, moving, meshes, missing, lost = [], [], [], [], 0
    for path, studio in zip(argv[2:], scene["studios"]):
        triangles = read_stl(path)
        owner, unmatched = assign(triangles, studio["parts"])
        lost += unmatched
        for instance in studio["instances"]:
            own = triangles[owner == instance["part"]]
            if not len(own):
                missing.append(instance["name"])
                continue
            t = np.array(instance["transform"], float).reshape(4, 4)
            names.append(instance["name"])
            moving.append(bool(instance["moving"]))
            meshes.append(own @ t[:3, :3].T + t[:3, 3])

    lap("read and match parts")
    joint = []  # for each side of the joint: the parts rigidly on that side that have round geometry on the axis
    for side in scene.get("joint_sides", []):
        found = {}
        for name in side:
            if name in names:
                rings = coaxial(meshes[names.index(name)].reshape(-1, 3), origin, axis)
                if rings:
                    found[name] = rings
        joint.append({"parts": [n for n in side if n in names], "round": found})

    still_ids = [i for i in range(len(names)) if not moving[i]]
    move_ids = [i for i in range(len(names)) if moving[i]]
    if not still_ids or not move_ids:
        json.dump({"error": "one side of the joint has no geometry", "missing": missing}, sys.stdout)
        return
    still_points, still_owner = [], []
    for i in still_ids:
        points, _ = sample(meshes[i], spacing, rng)
        still_points.append(points)
        still_owner.append(np.full(len(points), i))
    still_points, still_owner = np.vstack(still_points), np.concatenate(still_owner)
    tree = cKDTree(still_points)
    move_points, move_owner = [], []
    for i in move_ids:
        points, _ = sample(meshes[i], spacing, rng)
        move_points.append(points)
        move_owner.append(np.full(len(points), i))
    move_points, move_owner = np.vstack(move_points), np.concatenate(move_owner)

    lap("sample surfaces")
    angles = [float(a) for a in scene["angles"]]
    pairs = {}  # (moving, still) -> per angle: (distance, moving point, still point)
    low, high = still_points.min(axis=0) - RANGE, still_points.max(axis=0) + RANGE
    for angle in angles:
        turned = (move_points - origin) @ rotation(axis, angle).T + origin
        candidates = np.flatnonzero(np.all((turned >= low) & (turned <= high), axis=1))  # the rest is far from everything still
        turned, owners = turned[candidates], move_owner[candidates]
        distance, nearest = tree.query(turned, k=1, distance_upper_bound=RANGE, workers=-1)
        near = np.flatnonzero(np.isfinite(distance))
        key = owners[near] * len(names) + still_owner[nearest[near]]
        order = np.argsort(distance[near], kind="stable")
        _, first = np.unique(key[order], return_index=True)
        for row in near[order[first]]:
            k = (int(owners[row]), int(still_owner[nearest[row]]))
            pairs.setdefault(k, {})[angle] = (float(distance[row]), turned[row], still_points[nearest[row]])

    lap("sweep")
    centres = [m.mean(axis=1) for m in meshes]
    reaches = [np.sqrt(np.max(np.sum((m - c[:, None, :]) ** 2, axis=2), axis=1)) for m, c in zip(meshes, centres)]

    def refined(a, b, angle, location, coarse):
        """Re-measure one pair at one angle on a sampling eight times finer, around where they come closest."""
        reach = coarse + 5 * spacing
        turn = rotation(axis, angle)
        back = (location - origin) @ turn + origin  # the same place, seen from the moving part before it turned
        sides = []
        for part, spot, moved in ((a, back, True), (b, location, False)):
            close = meshes[part][np.linalg.norm(centres[part] - spot, axis=1) < reach + reaches[part]]
            if not len(close):
                return coarse, location
            if moved:
                close = (close - origin) @ turn.T + origin
            points, _ = sample(close, spacing / 8, rng)
            sides.append(points[np.linalg.norm(points - location, axis=1) < reach])
        if not len(sides[0]) or not len(sides[1]):
            return coarse, location
        distance, nearest = cKDTree(sides[1]).query(sides[0], k=1, workers=-1)
        best = int(np.argmin(distance))
        return float(distance[best]), (sides[0][best] + sides[1][nearest[best]]) / 2

    summary = []
    for (a, b), by_angle in pairs.items():
        worst = min(by_angle, key=lambda angle: by_angle[angle][0])
        distance, p, q = by_angle[worst]
        summary.append({"moving": names[a], "still": names[b], "a": a, "b": b, "min": distance, "angle": worst,
                        "where": ((p + q) / 2).tolist(), "close_at": [angle for angle in angles if angle in by_angle and by_angle[angle][0] < spacing],
                        "seen_at": len(by_angle)})
    summary.sort(key=lambda s: s["min"])
    touch = 1.2 * spacing / 8  # closer than the fine sampling can tell apart
    for item in summary[:REFINE_PAIRS]:  # the nearest pairs are re-measured finely at every position where they are near
        by_angle = pairs[(item["a"], item["b"])]
        fine = {}
        for angle, (coarse, p, q) in by_angle.items():
            fine[angle] = refined(item["a"], item["b"], angle, (p + q) / 2, coarse) if coarse < 3 * spacing else (coarse, (p + q) / 2)
        worst = min(fine, key=lambda angle: fine[angle][0])
        item.update(min=fine[worst][0], angle=worst, where=np.asarray(fine[worst][1]).tolist(), refined=True,
                    close_at=[angle for angle in angles if angle in fine and fine[angle][0] < touch])
    summary.sort(key=lambda s: s["min"])
    lap("refine")
    for item in summary:
        item.pop("a"), item.pop("b")
        item["min"], item["where"] = round(item["min"], 2), [round(v, 1) for v in item["where"]]
    json.dump({"parts": len(names), "moving": [names[i] for i in move_ids], "still": len(still_ids), "angles": angles,
               "spacing": spacing, "fine": spacing / 8, "touch": touch, "pairs": summary[:40], "joint": joint, "no_geometry": missing,
               "unmatched_bodies": lost, "points": int(len(still_points) + len(move_points)), "seconds": spent}, sys.stdout)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv)
