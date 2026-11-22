"""Small exact meshes for tests, as binary STL."""
import math
import struct


def stl(triangles) -> bytes:
    body = b"".join(struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0) for a, b, c in triangles)
    return b"\0" * 80 + struct.pack("<I", len(triangles)) + body


def tube(outer: float, inner: float, height: float, centre, axis: str = "z", sides: int = 72) -> bytes:
    """A closed tube standing on z = centre[2] (or lying along x), with outward-facing triangles."""
    cx, cy, cz = centre
    ring = [(math.cos(2 * math.pi * i / sides), math.sin(2 * math.pi * i / sides)) for i in range(sides)]
    triangles = []
    for i in range(sides):
        (ax, ay), (bx, by) = ring[i], ring[(i + 1) % sides]
        o0, o1 = (cx + outer * ax, cy + outer * ay), (cx + outer * bx, cy + outer * by)
        i0, i1 = (cx + inner * ax, cy + inner * ay), (cx + inner * bx, cy + inner * by)
        lo, hi = cz, cz + height
        triangles += [((*o0, lo), (*o1, lo), (*o1, hi)), ((*o0, lo), (*o1, hi), (*o0, hi)),      # outer wall
                      ((*i0, lo), (*i1, hi), (*i1, lo)), ((*i0, lo), (*i0, hi), (*i1, hi)),      # inner wall
                      ((*o0, hi), (*o1, hi), (*i1, hi)), ((*o0, hi), (*i1, hi), (*i0, hi)),      # top ring
                      ((*o0, lo), (*i1, lo), (*o1, lo)), ((*o0, lo), (*i0, lo), (*i1, lo))]      # bottom ring
    if axis == "x":  # turn z into x, keeping the triangles facing outward
        triangles = [tuple((p[2], p[0], p[1]) for p in triangle) for triangle in triangles]
    return stl(triangles)


def box(lo, hi) -> bytes:
    """A closed box between two corners."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    c = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return stl([(c[a], c[b], c[d]) for a, b, d in faces])


def merge(*meshes: bytes) -> bytes:
    """Several meshes as one STL file, the way a Part Studio with several parts is exported."""
    count = sum(struct.unpack_from("<I", m, 80)[0] for m in meshes)
    return b"\0" * 80 + struct.pack("<I", count) + b"".join(m[84:] for m in meshes)
