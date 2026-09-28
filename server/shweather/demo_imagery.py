"""Synthetic radar and satellite tiles for demo mode, in the formats RainViewer and NASA GIBS use.

The sky follows the demo scenario (a cold front arriving from the west) plus showers that
grow, drift north-east and decay, so the Radar tab's loop moves. Images are rendered with
the standard library only (a small PNG encoder), at half resolution and scaled up.
"""

from __future__ import annotations

import math
import random
import struct
import zlib

HOUR = 3600
SIZE = 256
STEP = 2                             # render every 2nd pixel, then repeat it

# dBZ -> RGBA, roughly RainViewer's "Universal Blue" scheme
RADAR_COLORS = [
    (10, (136, 221, 238, 170)), (18, (0, 170, 221, 200)), (25, (0, 119, 204, 210)),
    (32, (0, 68, 153, 220)), (38, (255, 238, 0, 230)), (44, (255, 170, 0, 235)),
    (50, (255, 68, 0, 240)), (55, (193, 0, 0, 245)), (60, (255, 170, 255, 250)),
]


def png(width: int, height: int, rgba_rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rgba_rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _hash(i: int, j: int, k: int = 0) -> float:
    h = (i * 374761393 + j * 668265263 + k * 2147483647) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return (h & 0xFFFF) / 0xFFFF


def noise(x: float, y: float, k: int = 0) -> float:
    """Smooth value noise in [0, 1]."""
    i, j = math.floor(x), math.floor(y)
    fx, fy = x - i, y - j
    fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    a, b = _hash(i, j, k), _hash(i + 1, j, k)
    c, d = _hash(i, j + 1, k), _hash(i + 1, j + 1, k)
    return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy


class DemoSky:
    def __init__(self, t0: float, lat0: float, lon0: float, front_at: float, seed: int = 7):
        self.lat0, self.lon0, self.tf = lat0, lon0, front_at
        rng = random.Random(seed)
        # Showers: (lat, lon, birth time, radius in degrees, peak dBZ). Some are near the
        # boat at start-up so the loop shows something straight away.
        self.cells = []
        for k in range(46):
            near = k < 10
            self.cells.append((lat0 + rng.uniform(-0.8, 0.8) if near else lat0 + rng.uniform(-3.0, 3.0),
                               lon0 + rng.uniform(-1.4, 0.6) if near else lon0 + rng.uniform(-4.5, 3.5),
                               t0 + rng.uniform(-3.5, 1.0) * HOUR if near else t0 + rng.uniform(-3, 10) * HOUR,
                               rng.uniform(0.08, 0.3), rng.uniform(30, 56)))
        self.drift = (0.12, 0.28)            # degrees per hour north, east

    def _front_x(self, t: float, lon: float) -> float:
        return (t - self.tf) / HOUR - (lon - self.lon0) * 2.0

    def active_cells(self, t: float, box: tuple[float, float, float, float]) -> list[tuple]:
        """Showers alive at time t that reach into box (south, west, north, east): (lat, lon, r, peak*life)."""
        s, w, n, e = box
        out = []
        for la, lo, born, r, peak in self.cells:
            age = (t - born) / HOUR
            if not 0 < age < 2.5:
                continue
            cla, clo = la + self.drift[0] * age, lo + self.drift[1] * age
            if s - 2 * r <= cla <= n + 2 * r and w - 3 * r <= clo <= e + 3 * r:
                out.append((cla, clo, r, peak * math.sin(math.pi * age / 2.5)))
        return out

    def dbz(self, t: float, lat: float, lon: float, cells: list[tuple] | None = None) -> float:
        x = self._front_x(t, lon)
        rain = 3.5 * math.exp(-(x / 1.2) ** 2) + 0.4 * math.exp(-((x - 4) / 3) ** 2)
        best = -99.0
        if rain > 0.02:
            rain *= 0.35 + 0.9 * noise(lat * 14 + t / 5400, lon * 11, 1)
            if rain > 0.05:
                best = 10 * math.log10(200 * rain ** 1.6)
        if cells is None:
            cells = self.active_cells(t, (lat, lon, lat, lon))
        coslat = math.cos(math.radians(lat))
        for cla, clo, r, strength in cells:
            d2 = ((lat - cla) / r) ** 2 + ((lon - clo) * coslat / r) ** 2
            if d2 < 4:
                best = max(best, strength * math.exp(-d2) * (0.75 + 0.5 * noise(lat * 40, lon * 40, 2)))
        return best

    def cloud(self, t: float, lat: float, lon: float, cells: list[tuple] | None = None) -> float:
        x = self._front_x(t, lon)
        n = 0.65 * noise(lat * 1.3 + t / 40000, lon * 1.3, 3) + 0.35 * noise(lat * 4, lon * 4 - t / 30000, 4)
        k = min(1.0, max(0.0, (n - 0.42) / 0.3))
        c = 0.75 * k * k * (3 - 2 * k) + 0.85 * math.exp(-(x / 3.5) ** 2)
        return max(0.0, min(1.0, c + max(0.0, self.dbz(t, lat, lon, cells)) / 70))


def _latlon(z: int, x: float, y: float) -> tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def _render(z: int, tx: int, ty: int, pixel) -> bytes:
    lats = [_latlon(z, tx, ty + (py + STEP / 2) / SIZE)[0] for py in range(0, SIZE, STEP)]
    lons = [_latlon(z, tx + (px + STEP / 2) / SIZE, ty)[1] for px in range(0, SIZE, STEP)]
    rows = []
    for lat in lats:
        row = b"".join(bytes(pixel(lat, lon)) * STEP for lon in lons)
        rows += [row] * STEP
    return png(SIZE, SIZE, rows)


def _box(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n, w = _latlon(z, x, y)
    s, e = _latlon(z, x + 1, y + 1)
    return s, w, n, e


def radar_tile(sky: DemoSky, t: float, z: int, x: int, y: int) -> bytes:
    cells = sky.active_cells(t, _box(z, x, y))

    def pixel(lat: float, lon: float):
        v = sky.dbz(t, lat, lon, cells)
        color = (0, 0, 0, 0)
        for level, c in RADAR_COLORS:
            if v < level:
                break
            color = c
        return color
    return _render(z, x, y, pixel)


# Like NASA GIBS "clean infrared": grey that brightens as it gets colder (warm ground dark,
# low and mid cloud lighter), then colour for the coldest cloud tops.
IR_COLD_TOPS = [(0.40, (60, 210, 235)), (0.75, (20, 90, 210)), (1.01, (40, 200, 70))]


def ir_tile(sky: DemoSky, t: float, z: int, x: int, y: int) -> bytes:
    cells = sky.active_cells(t, _box(z, x, y))

    def pixel(lat: float, lon: float):
        c = sky.cloud(t, lat, lon, cells)
        if c < 0.75:
            g = int(95 + 140 * c / 0.75)
            return (g, g, g, 255)
        k = (c - 0.75) / 0.25
        return (*next(col for lim, col in IR_COLD_TOPS if k < lim), 255)
    return _render(z, x, y, pixel)
