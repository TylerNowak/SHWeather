"""NOAA National Data Buoy Center observations.

``/data/latest_obs/latest_obs.txt`` holds the latest report from every station (buoys,
C-MAN, partner platforms) with its position in a single ~150 KB file: one request gives
the nearest real observations for any position. ``/data/realtime2/<id>.txt`` gives a
station's last 45 days.

Both are whitespace-separated tables with two '#' header lines (names, units); 'MM' marks
missing values. Speeds are m/s; we convert to knots.
"""

from __future__ import annotations

import calendar
import time

from ..geo import bearing_deg, distance_nm
from ..net import Http
from ..units import ms_to_kn
from . import ProviderError

MISSING = "MM"
TEXT_COLUMNS = {"STN"}  # station ids like 45007 must stay strings


def parse_table(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#"):
        raise ProviderError("NDBC: unexpected file format")
    header = lines[0].lstrip("#").split()
    rows = []
    for ln in lines[1:]:
        if ln.startswith("#"):
            continue
        cols = ln.split()
        if len(cols) != len(header):
            continue
        rec: dict = {}
        for name, raw in zip(header, cols, strict=True):
            if raw == MISSING:
                rec[name] = None
                continue
            if name in TEXT_COLUMNS:
                rec[name] = raw
                continue
            try:
                rec[name] = float(raw)
            except ValueError:
                rec[name] = raw
        rows.append(rec)
    return rows


def _ts(rec: dict) -> float | None:
    year = rec.get("YYYY") or rec.get("YY")
    try:
        y = int(year)
        if y < 100:
            y += 2000
        return float(calendar.timegm((y, int(rec["MM"]), int(rec["DD"]), int(rec["hh"]), int(rec["mm"]), 0)))
    except (TypeError, KeyError, ValueError):
        return None


def normalize(rec: dict) -> dict:
    return {
        "station": rec.get("STN"),
        "lat": rec.get("LAT"),
        "lon": rec.get("LON"),
        "time": _ts(rec),
        "wind_dir_deg": rec.get("WDIR"),
        "wind_speed_kn": None if rec.get("WSPD") is None else round(ms_to_kn(rec["WSPD"]), 1),
        "wind_gust_kn": None if rec.get("GST") is None else round(ms_to_kn(rec["GST"]), 1),
        "wave_height_m": rec.get("WVHT"),
        "dominant_period_s": rec.get("DPD"),
        "average_period_s": rec.get("APD"),
        "wave_dir_deg": rec.get("MWD"),
        "pressure_hpa": rec.get("PRES"),
        "pressure_tendency_hpa": rec.get("PTDY"),
        "air_temp_c": rec.get("ATMP"),
        "water_temp_c": rec.get("WTMP"),
        "dewpoint_c": rec.get("DEWP"),
        "visibility_nm": rec.get("VIS"),
        "tide_ft": rec.get("TIDE"),
    }


def nearest(stations: list[dict], lat: float, lon: float, radius_nm: float, limit: int = 5,
            max_age_s: float = 6 * 3600, now: float | None = None) -> list[dict]:
    now = now or time.time()
    out = []
    for s in stations:
        if s.get("lat") is None or s.get("lon") is None:
            continue
        if s.get("time") is None or now - s["time"] > max_age_s:
            continue
        d = distance_nm(lat, lon, s["lat"], s["lon"])
        if d <= radius_nm:
            out.append({**s, "distance_nm": round(d, 1), "bearing_deg": round(bearing_deg(lat, lon, s["lat"], s["lon"]))})
    out.sort(key=lambda s: s["distance_nm"])
    return out[:limit]


class NDBC:
    def __init__(self, http: Http, base_url: str = "https://www.ndbc.noaa.gov"):
        self.http = http
        self.base = base_url.rstrip("/")

    async def _text(self, path: str) -> str:
        resp = await self.http.get(self.base + path)
        if resp.status_code != 200:
            raise ProviderError(f"NDBC HTTP {resp.status_code} for {path}")
        return resp.text

    async def latest_all(self) -> list[dict]:
        return [normalize(r) for r in parse_table(await self._text("/data/latest_obs/latest_obs.txt"))]

    async def station_history(self, station: str, hours: float = 24) -> list[dict]:
        rows = [normalize(r) for r in parse_table(await self._text(f"/data/realtime2/{station.upper()}.txt"))]
        cutoff = time.time() - hours * 3600
        return sorted((r for r in rows if r["time"] and r["time"] >= cutoff), key=lambda r: r["time"])
