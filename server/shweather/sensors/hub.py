"""SensorHub: latest instrument values, derived true wind, and 1-minute aggregates.

All inputs (NMEA, Signal K, BME280) call ``update()`` with canonical metric names. The hub
keeps the newest value of each metric, derives true wind when only apparent wind is
available, and accumulates samples so the scheduler can persist one averaged row per
metric per minute (gentle on SD cards, and exactly what the nowcast needs).
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from ..analysis.wind import true_wind
from ..config import Settings
from ..geo import circular_mean, distance_nm
from ..units import pressure_to_sea_level
from . import nmea0183

log = logging.getLogger(__name__)

# How long a reading is considered current, by metric.
FRESH_S: dict[str, float] = {
    "lat": 300, "lon": 300,
    "aws_kn": 15, "awa_deg": 15, "tws_kn": 15, "twd_deg": 15, "twa_deg": 15,
    "sog_kn": 15, "cog_deg": 15, "stw_kn": 15,
    "heading_true_deg": 15, "heading_mag_deg": 15, "mag_variation_deg": 3600,
    "pressure_hpa": 900, "pressure_station_hpa": 900,
    "air_temp_c": 900, "water_temp_c": 900, "humidity_pct": 900, "dewpoint_c": 900,
    "cabin_temp_c": 900, "cabin_humidity_pct": 900,
}
DEFAULT_FRESH_S = 60

# Metrics persisted as 1-minute aggregates.
AGGREGATED = ["tws_kn", "twd_deg", "aws_kn", "pressure_hpa", "air_temp_c", "water_temp_c",
              "humidity_pct", "sog_kn", "cabin_temp_c"]
DIRECTIONAL = {"twd_deg", "awa_deg", "cog_deg", "heading_true_deg", "twa_deg"}
TRUE_WIND_KEYS = ("tws_kn", "twd_deg", "twa_deg")
DIRECT_TRUE_WIND_FRESH_S = 5.0
MIN_SOG_FOR_COG_HEADING_KN = 1.5  # GPS course is noise when (nearly) stationary


@dataclass
class Reading:
    value: float
    ts: float
    source: str


class SensorHub:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.clock = clock
        self.latest: dict[str, Reading] = {}
        self.sources: dict[str, dict] = {}
        self._acc: dict[str, list[float]] = defaultdict(list)
        self._last_saved_pos: tuple[float, float, float] | None = None
        self._direct_true_wind_ts = float("-inf")

    # ---------- input ----------

    def feed_nmea(self, line: str, source: str) -> None:
        values = nmea0183.parse(line)
        st = self.sources.setdefault(source, {"sentences": 0, "useful": 0})
        st["sentences"] += 1
        st["last_seen"] = self.clock()
        st["connected"] = True
        if values:
            st["useful"] += 1
            self.update(values, source)

    def mark_source(self, source: str, *, connected: bool, error: str | None = None) -> None:
        st = self.sources.setdefault(source, {"sentences": 0, "useful": 0})
        st["connected"] = connected
        if error:
            st["error"] = error
            st["error_at"] = self.clock()

    def update(self, values: dict, source: str, ts: float | None = None) -> None:
        ts = ts or self.clock()
        values = dict(values)
        if "pressure_hpa" in values and values["pressure_hpa"] is not None:
            raw = values["pressure_hpa"] + self.settings.sensors.pressure_offset_hpa
            if not 800 <= raw <= 1100:
                values.pop("pressure_hpa")  # reject garbage
            else:
                values["pressure_station_hpa"] = raw
                temp = values.get("air_temp_c")
                if temp is None:
                    temp = self.value("air_temp_c")
                values["pressure_hpa"] = round(
                    pressure_to_sea_level(raw, self.settings.sensors.pressure_altitude_m, temp), 2)
        for k, v in values.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            self.latest[k] = Reading(float(v), ts, source)
            if k in AGGREGATED:
                self._acc[k].append(float(v))
        if any(k in values for k in TRUE_WIND_KEYS):
            # The instruments (or Signal K) provide true wind themselves: use only that, so
            # we never average instrument and self-derived true wind together.
            self._direct_true_wind_ts = ts
            if "twa_deg" in values and "twd_deg" not in values:
                self._twd_from_twa(ts)
        elif ("aws_kn" in values or "awa_deg" in values) and not self._has_fresh_direct_true_wind(ts):
            self._derive_true_wind(ts)

    # ---------- derivations ----------

    def _heading(self) -> float | None:
        h = self.value("heading_true_deg")
        if h is not None:
            return h
        hm, var = self.value("heading_mag_deg"), self.value("mag_variation_deg")
        if hm is not None and var is not None:
            return (hm + var) % 360
        return None

    def _has_fresh_direct_true_wind(self, now: float) -> bool:
        return now - self._direct_true_wind_ts < DIRECT_TRUE_WIND_FRESH_S

    def _twd_from_twa(self, ts: float) -> None:
        twa, hdg = self.value("twa_deg"), self._heading()
        if twa is not None and hdg is not None:
            self._set_derived("twd_deg", (hdg + twa) % 360, ts)

    def _derive_true_wind(self, ts: float) -> None:
        aws, awa = self.value("aws_kn"), self.value("awa_deg")
        if aws is None or awa is None:
            return
        heading = self._heading()
        sog, cog = self.value("sog_kn"), self.value("cog_deg")
        stw = self.value("stw_kn")
        if heading is None and cog is not None and sog is not None and sog >= MIN_SOG_FOR_COG_HEADING_KN:
            heading = cog  # no compass: assume we point where we go (only meaningful when moving)
        if heading is None:
            return
        if sog is not None and cog is not None:
            tw = true_wind(aws, awa, sog, heading, cog)  # wind over ground: comparable to forecasts
        elif stw is not None:
            tw = true_wind(aws, awa, stw, heading)
        else:
            return
        self._set_derived("tws_kn", round(tw["tws_kn"], 2), ts)
        self._set_derived("twd_deg", round(tw["twd_deg"], 1), ts)
        self._set_derived("twa_deg", round(tw["twa_deg"], 1), ts)

    def _set_derived(self, key: str, value: float, ts: float) -> None:
        self.latest[key] = Reading(value, ts, "derived")
        if key in AGGREGATED:
            self._acc[key].append(value)

    # ---------- output ----------

    def value(self, metric: str, max_age_s: float | None = None) -> float | None:
        r = self.latest.get(metric)
        if r is None:
            return None
        limit = FRESH_S.get(metric, DEFAULT_FRESH_S) if max_age_s is None else max_age_s
        return r.value if self.clock() - r.ts <= limit else None

    def position(self) -> dict | None:
        lat, lon = self.latest.get("lat"), self.latest.get("lon")
        if lat and lon and self.clock() - max(lat.ts, lon.ts) <= FRESH_S["lat"]:
            return {"lat": lat.value, "lon": lon.value, "ts": min(lat.ts, lon.ts), "source": lat.source}
        return None

    def snapshot(self) -> dict:
        now = self.clock()
        out = {}
        for k, r in self.latest.items():
            if k in ("lat", "lon"):
                continue
            if now - r.ts <= FRESH_S.get(k, DEFAULT_FRESH_S):
                out[k] = {"value": round(r.value, 2), "age_s": round(now - r.ts, 1), "source": r.source}
        return out

    def flush_minute(self) -> list[tuple[float, str, float, str | None]]:
        """Aggregate the samples collected since the last flush into DB rows."""
        now = self.clock()
        rows = []
        for metric, vals in self._acc.items():
            if not vals:
                continue
            v = circular_mean(vals) if metric in DIRECTIONAL else sum(vals) / len(vals)
            if v is None:
                continue
            rows.append((now, metric, round(v, 2), "onboard"))
            if metric == "tws_kn":
                rows.append((now, "tws_max_kn", round(max(vals), 2), "onboard"))
        self._acc.clear()
        return rows

    def position_to_save(self, min_move_nm: float = 0.05, max_interval_s: float = 600) -> dict | None:
        pos = self.position()
        if not pos:
            return None
        last = self._last_saved_pos
        if (last is None or pos["ts"] - last[2] >= max_interval_s
                or distance_nm(last[0], last[1], pos["lat"], pos["lon"]) >= min_move_nm):
            self._last_saved_pos = (pos["lat"], pos["lon"], pos["ts"])
            return pos
        return None
