"""Build web/data/basemap.json: land and lakes for the Radar tab's offline map.

The source is Natural Earth 1:50m (public domain, https://www.naturalearthdata.com/),
which is detailed enough for a 10-400 nm weather map and small enough for a phone:
coordinates are quantized to 1/100 degree (about 1 km) and delta-encoded.

    python tools/build_basemap.py ne_50m_land.geojson ne_50m_lakes.geojson web/data/basemap.json

Download the two files from
https://github.com/nvkelso/natural-earth-vector/tree/master/geojson

Output format (all integers in units of 1/Q degree):
    {"q": 100, "land": [poly, ...], "lakes": [poly, ...]}
    poly = [minx, miny, maxx, maxy, ring, ring, ...]   (first ring outer, then holes)
    ring = [x0, y0, dx1, dy1, dx2, dy2, ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

Q = 100            # units per degree
TOLERANCE = 0.012  # Douglas-Peucker tolerance in degrees (~1.3 km); well under the data's own detail
MIN_AREA = 0.02    # square degrees; drops islets smaller than ~15 x 15 km outside lakes


def simplify(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker on a closed ring (iterative, keeps the endpoints)."""
    if len(points) < 5:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / norm if norm else ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            if d > best:
                best, idx = d, i
        if idx >= 0 and best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep, strict=True) if k]


def area(ring: list[tuple[float, float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True))) / 2


def encode_ring(ring: list[tuple[float, float]]) -> list[int]:
    out: list[int] = []
    px = py = 0
    first = True
    for x, y in ring:
        ix, iy = round(x * Q), round(y * Q)
        if not first and ix == px and iy == py:
            continue
        out += [ix, iy] if first else [ix - px, iy - py]
        px, py, first = ix, iy, False
    return out


def polygons(path: Path, min_area: float):
    data = json.loads(path.read_text(encoding="utf-8"))
    for feat in data["features"]:
        geom = feat["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for poly in polys:
            rings = []
            for i, ring in enumerate(poly):
                pts = [(float(x), float(y)) for x, y in ring]
                if pts and pts[0] == pts[-1]:
                    pts = pts[:-1]
                if area(pts) < (min_area if i == 0 else min_area / 4):
                    if i == 0:
                        break
                    continue
                pts = simplify(pts + [pts[0]], TOLERANCE)[:-1]
                if len(pts) >= 3:
                    rings.append(pts)
            if rings:
                xs = [x for x, _ in rings[0]]
                ys = [y for _, y in rings[0]]
                yield [round(min(xs) * Q), round(min(ys) * Q), round(max(xs) * Q), round(max(ys) * Q),
                       *[encode_ring(r) for r in rings]]


def main(land: str, lakes: str, out: str) -> None:
    result = {
        "source": "Natural Earth 1:50m land and lakes (public domain), simplified",
        "q": Q,
        "land": list(polygons(Path(land), MIN_AREA)),
        "lakes": list(polygons(Path(lakes), MIN_AREA / 2)),
    }
    text = json.dumps(result, separators=(",", ":"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text, encoding="utf-8")
    n = sum(len(r) // 2 for group in ("land", "lakes") for p in result[group] for r in p[4:])
    print(f"{out}: {len(result['land'])} land and {len(result['lakes'])} lake polygons, "
          f"{n} points, {len(text) / 1024:.0f} KB")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
