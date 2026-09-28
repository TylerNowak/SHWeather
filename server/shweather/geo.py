"""Geodesy helpers, forecast grids and inverse-distance interpolation."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

EARTH_RADIUS_NM = 3440.065


def normalize_lon(lon: float) -> float:
    return ((lon + 180.0) % 360.0) - 180.0


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(normalize_lon(lon2 - lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(normalize_lon(lon2 - lon1))
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass_point(deg: float | None, points: int = 16) -> str | None:
    if deg is None:
        return None
    names16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
               "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    if points == 8:
        return names16[(int((deg % 360) / 45 + 0.5) % 8) * 2]
    return names16[int((deg % 360) / 22.5 + 0.5) % 16]


@dataclass(frozen=True)
class BBox:
    """Lat/lon box. Longitudes are 'unwrapped' around the box centre, so a box crossing the
    antimeridian is stored as e.g. 179.5 .. 180.5 rather than -180 .. 179.75."""

    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float

    def _lon(self, lon: float) -> float:
        centre = (self.min_lon + self.max_lon) / 2
        return centre + normalize_lon(lon - centre)

    def contains(self, lat: float, lon: float, margin_deg: float = 0.0) -> bool:
        lon = self._lon(lon)
        return (self.min_lat - margin_deg <= lat <= self.max_lat + margin_deg
                and self.min_lon - margin_deg <= lon <= self.max_lon + margin_deg)

    def edge_distance_deg(self, lat: float, lon: float) -> float:
        """Distance (deg) from a point inside the box to its nearest edge; negative if outside."""
        lon = self._lon(lon)
        return min(lat - self.min_lat, self.max_lat - lat, lon - self.min_lon, self.max_lon - lon)


def snap(value: float, step: float) -> float:
    return round(round(value / step) * step, 6)


def make_grid(lat: float, lon: float, steps: int, spacing_deg: float,
              lon_steps: int | None = None) -> list[tuple[float, float]]:
    """(2*steps+1) x (2*lon_steps+1) points centred on the position snapped to `spacing_deg`.

    Snapping keeps successive grids aligned, so a boat drifting a few miles does not force
    every point to be re-requested and cached points line up between refreshes.
    """
    clat, clon = snap(lat, spacing_deg), snap(lon, spacing_deg)
    lsteps = steps if lon_steps is None else lon_steps
    pts = []
    for i in range(-steps, steps + 1):
        la = clat + i * spacing_deg
        if la > 89.9 or la < -89.9:
            continue
        for j in range(-lsteps, lsteps + 1):
            pts.append((round(la, 6), round(normalize_lon(clon + j * spacing_deg), 6)))
    return pts


def grid_for_radius(lat: float, lon: float, radius_nm: float, max_points: int = 400,
                    min_spacing_deg: float = 0.25) -> tuple[list[tuple[float, float]], float]:
    """A grid covering `radius_nm` around a position, coarsened to stay under max_points."""
    radius_lat = radius_nm / 60.0
    radius_lon = radius_lat / max(0.2, math.cos(math.radians(lat)))  # meridians converge
    spacing = min_spacing_deg
    while True:
        steps = max(1, math.ceil(radius_lat / spacing))
        lon_steps = max(1, math.ceil(radius_lon / spacing))
        if (2 * steps + 1) * (2 * lon_steps + 1) <= max_points:
            return make_grid(lat, lon, steps, spacing, lon_steps), spacing
        spacing *= 1.5


def bbox_of(points: Iterable[tuple[float, float]], center_lon: float | None = None) -> BBox:
    pts = list(points)
    ref = pts[0][1] if center_lon is None else center_lon
    lons = [ref + normalize_lon(p[1] - ref) for p in pts]  # unwrap across the antimeridian
    return BBox(min(p[0] for p in pts), max(p[0] for p in pts), min(lons), max(lons))


def idw_weights(lat: float, lon: float, points: Sequence[tuple[float, float]],
                k: int = 4, power: float = 2.0) -> list[tuple[int, float]]:
    """Indices and normalised inverse-distance weights of the k nearest points."""
    d = sorted((distance_nm(lat, lon, p[0], p[1]), i) for i, p in enumerate(points))[:k]
    if d and d[0][0] < 0.05:  # effectively on top of a grid point
        return [(d[0][1], 1.0)]
    raw = [(i, 1.0 / (dist ** power)) for dist, i in d]
    total = sum(w for _, w in raw)
    return [(i, w / total) for i, w in raw]


def weighted_scalar(values: Sequence[float | None], weights: Sequence[float]) -> float | None:
    num = den = 0.0
    for v, w in zip(values, weights, strict=True):
        if v is not None:
            num += v * w
            den += w
    return num / den if den > 0 else None


def weighted_direction(dirs: Sequence[float | None], weights: Sequence[float],
                       mags: Sequence[float | None] | None = None) -> float | None:
    """Weighted circular mean; optionally weighted by magnitude (e.g. wind speed)."""
    x = y = 0.0
    any_val = False
    for i, (d, w) in enumerate(zip(dirs, weights, strict=True)):
        if d is None:
            continue
        m = 1.0 if mags is None or mags[i] is None else max(mags[i], 0.01)
        r = math.radians(d)
        x += math.sin(r) * w * m
        y += math.cos(r) * w * m
        any_val = True
    if not any_val or (abs(x) < 1e-12 and abs(y) < 1e-12):
        return None
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def circular_mean(dirs: Iterable[float]) -> float | None:
    ds = list(dirs)
    return weighted_direction(ds, [1.0] * len(ds)) if ds else None


def angle_diff(a: float, b: float) -> float:
    """Signed smallest difference a - b in degrees, in (-180, 180]."""
    d = (a - b + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d
