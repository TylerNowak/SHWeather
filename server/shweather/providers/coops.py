"""NOAA CO-OPS (Tides & Currents) predictions for US coastal waters.

Data API: https://api.tidesandcurrents.noaa.gov/api/prod/
Metadata API (station lists): https://api.tidesandcurrents.noaa.gov/mdapi/prod/

Tide heights are metres above MLLW; tidal current speeds are knots (``units=english``),
positive = flood, negative = ebb. The Great Lakes have no tide predictions (the nearest
station check simply finds nothing).
"""

from __future__ import annotations

import calendar
import datetime as dt

from ..geo import bearing_deg, distance_nm
from ..net import Http
from . import NotApplicable, ProviderError

APP = "SHWeather"


def parse_gmt(s: str) -> float:
    return float(calendar.timegm(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").timetuple()))


def normalize_stations(payload: dict) -> list[dict]:
    out = []
    for s in payload.get("stations") or []:
        lat, lon = s.get("lat"), s.get("lng", s.get("lon"))
        if lat is None or lon is None:
            continue
        out.append({"id": str(s.get("id")), "name": s.get("name"), "lat": float(lat), "lon": float(lon),
                    "state": s.get("state")})
    return out


def nearest_station(stations: list[dict], lat: float, lon: float, radius_nm: float) -> dict:
    best = None
    for s in stations:
        d = distance_nm(lat, lon, s["lat"], s["lon"])
        if d <= radius_nm and (best is None or d < best[0]):
            best = (d, s)
    if not best:
        raise NotApplicable(f"no CO-OPS station within {radius_nm} nm")
    d, s = best
    return {**s, "distance_nm": round(d, 1), "bearing_deg": round(bearing_deg(lat, lon, s["lat"], s["lon"]))}


def normalize_hilo(payload: dict) -> list[dict]:
    if "error" in payload:
        raise ProviderError(f"CO-OPS: {payload['error'].get('message')}")
    return [{"time": parse_gmt(p["t"]), "height_m": float(p["v"]), "type": "high" if p.get("type") == "H" else "low"}
            for p in payload.get("predictions") or [] if p.get("v") not in (None, "")]


def normalize_curve(payload: dict) -> list[dict]:
    if "error" in payload:
        raise ProviderError(f"CO-OPS: {payload['error'].get('message')}")
    return [{"time": parse_gmt(p["t"]), "height_m": float(p["v"])}
            for p in payload.get("predictions") or [] if p.get("v") not in (None, "")]


def normalize_currents(payload: dict) -> list[dict]:
    if "error" in payload:
        raise ProviderError(f"CO-OPS: {payload['error'].get('message')}")
    cp = (payload.get("current_predictions") or {}).get("cp") or []
    out = []
    for p in cp:
        v = p.get("Velocity_Major")
        out.append({
            "time": parse_gmt(p["Time"]),
            "type": (p.get("Type") or "").lower() or ("flood" if (v or 0) > 0 else "ebb" if (v or 0) < 0 else "slack"),
            "speed_kn": None if v is None else abs(float(v)),
            "velocity_kn": None if v is None else float(v),
            "flood_dir_deg": p.get("meanFloodDir"),
            "ebb_dir_deg": p.get("meanEbbDir"),
        })
    return out


class Coops:
    def __init__(self, http: Http, base_url: str = "https://api.tidesandcurrents.noaa.gov"):
        self.http = http
        self.base = base_url.rstrip("/")

    async def _json(self, path: str, params: dict) -> dict:
        resp = await self.http.get(self.base + path, params=params)
        if resp.status_code != 200:
            raise ProviderError(f"CO-OPS HTTP {resp.status_code} for {path}")
        return resp.json()

    async def stations(self, kind: str) -> list[dict]:
        """kind: 'tidepredictions' or 'currentpredictions'."""
        return normalize_stations(await self._json("/mdapi/prod/webapi/stations.json", {"type": kind}))

    def _range_params(self, start: float, hours: int) -> dict:
        begin = dt.datetime.fromtimestamp(start, dt.UTC).strftime("%Y%m%d %H:%M")
        return {"begin_date": begin, "range": hours, "time_zone": "gmt", "format": "json", "application": APP}

    async def tide_hilo(self, station: str, start: float, hours: int = 72) -> list[dict]:
        return normalize_hilo(await self._json("/api/prod/datagetter", {
            **self._range_params(start, hours), "station": station, "product": "predictions",
            "datum": "MLLW", "units": "metric", "interval": "hilo"}))

    async def tide_curve(self, station: str, start: float, hours: int = 48) -> list[dict]:
        return normalize_curve(await self._json("/api/prod/datagetter", {
            **self._range_params(start, hours), "station": station, "product": "predictions",
            "datum": "MLLW", "units": "metric", "interval": "h"}))

    async def currents(self, station: str, start: float, hours: int = 48) -> list[dict]:
        return normalize_currents(await self._json("/api/prod/datagetter", {
            **self._range_params(start, hours), "station": station, "product": "currents_predictions",
            "units": "english", "interval": "MAX_SLACK"}))
