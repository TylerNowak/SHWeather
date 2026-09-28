"""Canonical hourly forecast series.

A series is a plain dict ``{"time": [unix_s, ...], "<field>": [value|None, ...], ...}``
with every list the same length. Plain dicts keep storage (JSON in SQLite) and the API
trivial; this module documents the fields and provides the few operations we need.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable

# field name -> human description (canonical units, see docs/ARCHITECTURE.md)
FIELDS: dict[str, str] = {
    "wind_speed_kn": "Sustained wind at 10 m, knots",
    "wind_dir_deg": "Wind direction (from), degrees true",
    "wind_gust_kn": "Wind gusts at 10 m, knots",
    "pressure_hpa": "Mean sea level pressure, hPa",
    "temp_c": "Air temperature at 2 m, C",
    "dewpoint_c": "Dew point at 2 m, C",
    "humidity_pct": "Relative humidity, %",
    "precip_mm": "Precipitation in the preceding hour, mm",
    "precip_prob_pct": "Probability of precipitation, %",
    "cloud_pct": "Total cloud cover, %",
    "visibility_m": "Visibility, metres",
    "cape_jkg": "Convective available potential energy, J/kg (squall/thunderstorm fuel)",
    "weather_code": "WMO weather code",
    "is_day": "1 during daylight",
    "wave_height_m": "Significant wave height (combined sea), m",
    "wave_dir_deg": "Mean wave direction (from), degrees true",
    "wave_period_s": "Mean wave period, s",
    "wind_wave_height_m": "Wind-sea height, m",
    "wind_wave_dir_deg": "Wind-sea direction (from), degrees true",
    "wind_wave_period_s": "Wind-sea period, s",
    "swell_height_m": "Primary swell height, m",
    "swell_dir_deg": "Primary swell direction (from), degrees true",
    "swell_period_s": "Primary swell period, s",
    "current_speed_kn": "Ocean current speed, knots",
    "current_dir_deg": "Ocean current direction (towards), degrees true",
    "sst_c": "Sea surface temperature, C",
}

DIRECTION_FIELDS = {f for f in FIELDS if f.endswith("_dir_deg")}

# Which magnitude field weights each direction field during interpolation.
DIRECTION_MAGNITUDE = {
    "wind_dir_deg": "wind_speed_kn",
    "wave_dir_deg": "wave_height_m",
    "wind_wave_dir_deg": "wind_wave_height_m",
    "swell_dir_deg": "swell_height_m",
    "current_dir_deg": "current_speed_kn",
}

MARINE_FIELDS = {"wave_height_m", "wave_dir_deg", "wave_period_s", "wind_wave_height_m",
                 "wind_wave_dir_deg", "wind_wave_period_s", "swell_height_m", "swell_dir_deg",
                 "swell_period_s", "current_speed_kn", "current_dir_deg", "sst_c"}


def empty(times: Iterable[int]) -> dict:
    return {"time": list(times)}


def has_data(series: dict, field: str) -> bool:
    return any(v is not None for v in series.get(field) or [])


def merge(base: dict, other: dict, fields: Iterable[str] | None = None, overwrite: bool = False) -> dict:
    """Merge `other` into `base` by timestamp. Only fills gaps unless overwrite=True."""
    idx = {t: i for i, t in enumerate(base["time"])}
    n = len(base["time"])
    for f in fields if fields is not None else [k for k in other if k != "time"]:
        if f not in other:
            continue
        col = base.setdefault(f, [None] * n)
        for t, v in zip(other["time"], other[f], strict=False):
            i = idx.get(t)
            if i is not None and v is not None and (overwrite or col[i] is None):
                col[i] = v
    return base


def window(series: dict, start: float, end: float) -> dict:
    """Sub-series with start <= time < end."""
    t = series["time"]
    i, j = bisect.bisect_left(t, start), bisect.bisect_left(t, end)
    return {k: v[i:j] for k, v in series.items()}


def index_at(series: dict, ts: float) -> int | None:
    """Index of the hour containing ts (hourly steps), or None if out of range."""
    t = series.get("time") or []
    if not t or ts < t[0] - 1800 or ts > t[-1] + 3600:
        return None
    i = bisect.bisect_right(t, ts) - 1
    return max(0, min(i, len(t) - 1))


def value_at(series: dict, field: str, ts: float) -> float | None:
    i = index_at(series, ts)
    col = series.get(field)
    if i is None or not col:
        return None
    return col[i]


def row(series: dict, i: int) -> dict:
    return {k: (v[i] if i < len(v) else None) for k, v in series.items()}
