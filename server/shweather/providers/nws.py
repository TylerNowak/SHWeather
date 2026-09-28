"""US National Weather Service API (api.weather.gov).

Used for:
- Active alerts at the boat's position (Small Craft Advisory, Gale Warning, Special Marine
  Warning, ...).
- Gridpoint forecasts: forecaster-edited wind, and **wave grids for the Great Lakes and US
  coastal waters**, which the global ocean wave models behind Open-Meteo do not cover.
- The text forecast for the boat's marine zone (NSH nearshore / CWF coastal waters / GLF
  open lakes), which is what the Coast Guard reads on VHF.

The API requires a User-Agent that identifies the application and a contact; set
``sources.contact`` in config.yaml. Points outside NWS coverage return 404 and are
reported as NotApplicable.

Status: written against the published API format; exercised in tests with fixtures, not
yet against the live service. Please report mismatches.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import re

from ..net import Http
from ..units import length_to_m, speed_to_kn
from . import NotApplicable, ProviderError

log = logging.getLogger(__name__)

GEOJSON = {"Accept": "application/geo+json"}

# Gridpoint property -> (canonical field, kind)
GRID_FIELDS: dict[str, tuple[str, str]] = {
    "windSpeed": ("wind_speed_kn", "speed"),
    "windGust": ("wind_gust_kn", "speed"),
    "windDirection": ("wind_dir_deg", "angle"),
    "waveHeight": ("wave_height_m", "length"),
    "wavePeriod": ("wave_period_s", "time"),
    "waveDirection": ("wave_dir_deg", "angle"),
    "primarySwellHeight": ("swell_height_m", "length"),
    "primarySwellDirection": ("swell_dir_deg", "angle"),
    "windWaveHeight": ("wind_wave_height_m", "length"),
}

MARINE_EVENTS = {
    "Small Craft Advisory", "Gale Warning", "Gale Watch", "Storm Warning", "Storm Watch",
    "Hurricane Force Wind Warning", "Hurricane Force Wind Watch", "Special Marine Warning",
    "Marine Weather Statement", "Hazardous Seas Warning", "Hazardous Seas Watch",
    "Brisk Wind Advisory", "Dense Fog Advisory", "Heavy Freezing Spray Warning",
    "Freezing Spray Advisory", "Low Water Advisory", "Beach Hazards Statement",
    "Rip Current Statement", "High Surf Advisory", "High Surf Warning", "Tropical Storm Warning",
    "Hurricane Warning", "Tropical Storm Watch", "Hurricane Watch", "Storm Surge Warning",
    "Coastal Flood Advisory", "Coastal Flood Warning", "Lakeshore Flood Advisory",
    "Lakeshore Flood Warning", "Severe Thunderstorm Warning", "Tornado Warning",
}

# Two-letter prefixes of NWS marine zone ids (e.g. LMZ740 = Lake Michigan nearshore).
MARINE_PREFIXES = {"AM", "AN", "GM", "LC", "LE", "LH", "LM", "LO", "LS", "SL",
                   "PZ", "PK", "PH", "PM", "PS"}
GREAT_LAKES = {"LE", "LH", "LM", "LO", "LS"}

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")
_UGC_START = re.compile(r"^[A-Z]{2}[CZ]\d{3}")
_UGC_END = re.compile(r"\d{6}-\s*$")
_UGC_TOKEN = re.compile(r"^(?:([A-Z]{2}[CZ]))?(\d{3})(?:>(\d{3}))?$")


def parse_duration_hours(d: str) -> int:
    m = _DURATION.match(d)
    if not m:
        raise ValueError(f"bad ISO duration {d!r}")
    days, hours, minutes = (int(x) if x else 0 for x in m.groups())
    return max(1, days * 24 + hours + (1 if minutes else 0))


def expand_values(values: list[dict]) -> dict[int, float]:
    """Expand NWS ``validTime`` intervals ("2026-09-28T15:00:00+00:00/PT3H") to hourly values."""
    out: dict[int, float] = {}
    for v in values:
        if v.get("value") is None:
            continue
        start_s, _, dur = v["validTime"].partition("/")
        start = int(dt.datetime.fromisoformat(start_s).timestamp())
        start -= start % 3600
        for h in range(parse_duration_hours(dur or "PT1H")):
            out[start + h * 3600] = float(v["value"])
    return out


def _convert(value: float, kind: str, uom: str) -> float | None:
    u = (uom or "").lower()
    if kind == "speed":
        return speed_to_kn(value, u)
    if kind == "length":
        return length_to_m(value, u or "m")
    return value


def normalize_gridpoint(payload: dict) -> dict:
    props = payload.get("properties") or {}
    columns: dict[str, dict[int, float]] = {}
    for src, (dst, kind) in GRID_FIELDS.items():
        layer = props.get(src)
        if not layer or not layer.get("values"):
            continue
        uom = layer.get("uom", "")
        columns[dst] = {t: _convert(v, kind, uom) for t, v in expand_values(layer["values"]).items()}
    times = sorted({t for col in columns.values() for t in col})
    series: dict = {"time": times}
    for f, col in columns.items():
        series[f] = [None if col.get(t) is None else round(col[t], 2) for t in times]
    series["_updated"] = props.get("updateTime")
    return series


def normalize_alert(feature: dict) -> dict:
    p = feature.get("properties") or {}
    event = p.get("event") or ""
    return {
        "id": p.get("id") or feature.get("id"),
        "event": event,
        "headline": p.get("headline"),
        "severity": p.get("severity"),
        "urgency": p.get("urgency"),
        "certainty": p.get("certainty"),
        "onset": p.get("onset") or p.get("effective"),
        "ends": p.get("ends") or p.get("expires"),
        "area": p.get("areaDesc"),
        "description": p.get("description"),
        "instruction": p.get("instruction"),
        "sender": p.get("senderName"),
        "marine": event in MARINE_EVENTS,
        "source": "nws",
    }


def parse_ugc(text: str) -> set[str]:
    """Expand a UGC string like 'LMZ740>742-744-282100-' into {'LMZ740','LMZ741','LMZ742','LMZ744'}."""
    zones: set[str] = set()
    prefix = None
    for tok in text.replace("\n", "").replace(" ", "").split("-"):
        if not tok or re.fullmatch(r"\d{6}", tok):
            continue
        m = _UGC_TOKEN.match(tok)
        if not m:
            continue
        if m.group(1):
            prefix = m.group(1)
        if not prefix:
            continue
        a = int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        zones.update(f"{prefix}{n:03d}" for n in range(a, b + 1))
    return zones


def extract_zone_text(product_text: str, zone_id: str) -> str | None:
    """Return the section of a multi-zone marine product that applies to `zone_id`."""
    zone_id = zone_id.upper()
    for segment in re.split(r"^\s*\$\$\s*$", product_text, flags=re.M):
        lines = segment.strip("\n").splitlines()
        for i, line in enumerate(lines):
            if _UGC_START.match(line.strip()):
                ugc = [line.strip()]
                j = i
                while not _UGC_END.search(lines[j]) and j + 1 < len(lines):
                    j += 1
                    ugc.append(lines[j].strip())
                if zone_id in parse_ugc("".join(ugc)):
                    return "\n".join(lines[j + 1:]).strip()
                break
    return None


def is_marine_zone(zone_id: str) -> bool:
    return len(zone_id) == 6 and zone_id[2] == "Z" and zone_id[:2] in MARINE_PREFIXES


class NWS:
    def __init__(self, http: Http, base_url: str = "https://api.weather.gov"):
        self.http = http
        self.base = base_url.rstrip("/")

    async def _json(self, path_or_url: str, params: dict | None = None, essential: bool = False) -> dict:
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        resp = await self.http.get(url, params=params, headers=GEOJSON, essential=essential)
        if resp.status_code == 404:
            raise NotApplicable(f"NWS has no data for {url}")
        if resp.status_code != 200:
            detail = ""
            with contextlib.suppress(Exception):
                detail = resp.json().get("detail", "")
            if resp.status_code == 400 and "out of bounds" in detail.lower():
                raise NotApplicable(detail)
            raise ProviderError(f"NWS HTTP {resp.status_code} for {url}: {detail}")
        return resp.json()

    async def point(self, lat: float, lon: float) -> dict:
        data = await self._json(f"/points/{lat:.4f},{lon:.4f}")
        p = data.get("properties") or {}
        return {
            "grid_id": p.get("gridId"),
            "grid_x": p.get("gridX"),
            "grid_y": p.get("gridY"),
            "grid_url": p.get("forecastGridData"),
            "cwa": p.get("cwa") or p.get("gridId"),
            "forecast_zone": (p.get("forecastZone") or "").rsplit("/", 1)[-1] or None,
            "time_zone": p.get("timeZone"),
        }

    async def alerts(self, lat: float, lon: float) -> list[dict]:
        # Safety-critical and only a few KB: allowed to use the bandwidth reserve.
        data = await self._json("/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"}, essential=True)
        alerts = [normalize_alert(f) for f in data.get("features") or []]
        # Marine hazards first, then by severity.
        order = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
        alerts.sort(key=lambda a: (not a["marine"], order.get(a["severity"] or "", 4)))
        return alerts

    async def gridpoint(self, grid_url: str) -> dict:
        return normalize_gridpoint(await self._json(grid_url))

    async def marine_zone(self, lat: float, lon: float) -> dict | None:
        data = await self._json("/zones", params={"point": f"{lat:.4f},{lon:.4f}"})
        candidates = []
        for f in data.get("features") or []:
            p = f.get("properties") or {}
            zid = (p.get("id") or "").upper()
            ztype = (p.get("type") or "").lower()
            if ztype in ("marine", "coastal", "offshore") or is_marine_zone(zid):
                rank = {"coastal": 0, "marine": 1, "offshore": 2}.get(ztype, 1)
                offices = p.get("cwa") or []
                if isinstance(offices, str):
                    offices = [offices]
                candidates.append((rank, {"id": zid, "name": p.get("name"), "type": ztype, "offices": offices}))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]

    async def zone_offices(self, zone_id: str) -> list[str]:
        for ztype in ("marine", "coastal", "offshore"):
            try:
                data = await self._json(f"/zones/{ztype}/{zone_id}")
            except NotApplicable:
                continue
            offices = (data.get("properties") or {}).get("cwa") or []
            return [offices] if isinstance(offices, str) else list(offices)
        return []

    async def marine_text(self, zone_id: str, offices: list[str]) -> dict:
        zone_id = zone_id.upper()
        attempts: list[tuple[str, str]] = []
        for wfo in offices:
            attempts += [("NSH", wfo), ("CWF", wfo)]
        if zone_id[:2] in GREAT_LAKES:
            attempts.append(("GLF", zone_id[:2]))
        for ptype, loc in attempts:
            try:
                listing = await self._json(f"/products/types/{ptype}/locations/{loc}")
            except (NotApplicable, ProviderError):
                continue
            graph = listing.get("@graph") or []
            if not graph:
                continue
            latest = graph[0]
            product = await self._json(latest.get("@id") or f"/products/{latest['id']}")
            text = product.get("productText") or ""
            section = extract_zone_text(text, zone_id)
            if section:
                return {"zone": zone_id, "product": ptype, "office": loc,
                        "issued": product.get("issuanceTime"), "text": section}
        raise NotApplicable(f"no marine text product found for {zone_id}")
