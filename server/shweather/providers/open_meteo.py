"""Open-Meteo forecast and marine APIs.

Docs: https://open-meteo.com/en/docs and https://open-meteo.com/en/docs/marine-weather-api

Both endpoints accept comma-separated coordinate lists, returning a JSON array (one object
per location) instead of a single object. We use that to fetch a whole grid around the
boat in one or two requests.

Notes
- ``cell_selection=sea`` asks the forecast API to prefer grid cells over water; wind over
  water is typically stronger and steadier than over the adjacent land cell.
- ``past_days=1`` includes yesterday so onboard observations can be compared with the
  forecast for the same hours (see analysis.nowcast).
- The marine models are ocean models: for the Great Lakes and inland lakes they return
  nulls, and the NWS provider fills in waves for US lakes.
- The free API is for non-commercial use; set ``sources.open_meteo_apikey`` for the
  commercial endpoint, or point the URLs at your own Open-Meteo instance.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ..net import Http
from ..units import length_to_m, speed_to_kn
from . import ProviderError

log = logging.getLogger(__name__)

FORECAST_VARS: dict[str, str] = {
    "wind_speed_10m": "wind_speed_kn",
    "wind_direction_10m": "wind_dir_deg",
    "wind_gusts_10m": "wind_gust_kn",
    "pressure_msl": "pressure_hpa",
    "temperature_2m": "temp_c",
    "dew_point_2m": "dewpoint_c",
    "relative_humidity_2m": "humidity_pct",
    "precipitation": "precip_mm",
    "precipitation_probability": "precip_prob_pct",
    "cloud_cover": "cloud_pct",
    "visibility": "visibility_m",
    "cape": "cape_jkg",
    "weather_code": "weather_code",
    "is_day": "is_day",
}

MARINE_VARS: dict[str, str] = {
    "wave_height": "wave_height_m",
    "wave_direction": "wave_dir_deg",
    "wave_period": "wave_period_s",
    "wind_wave_height": "wind_wave_height_m",
    "wind_wave_direction": "wind_wave_dir_deg",
    "wind_wave_period": "wind_wave_period_s",
    "swell_wave_height": "swell_height_m",
    "swell_wave_direction": "swell_dir_deg",
    "swell_wave_period": "swell_period_s",
    "ocean_current_velocity": "current_speed_kn",
    "ocean_current_direction": "current_dir_deg",
    "sea_surface_temperature": "sst_c",
}

SPEED_FIELDS = {"wind_speed_kn", "wind_gust_kn", "current_speed_kn"}
LENGTH_FIELDS = {"wave_height_m", "wind_wave_height_m", "swell_height_m"}

MAX_POINTS_PER_REQUEST = 50


def _fmt(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".")


def normalize_location(obj: dict, var_map: dict[str, str]) -> dict:
    """Convert one Open-Meteo location object into a canonical series."""
    if obj.get("error"):
        raise ProviderError(f"open-meteo: {obj.get('reason')}")
    hourly = obj.get("hourly") or {}
    units = obj.get("hourly_units") or {}
    times = hourly.get("time") or []
    out: dict = {"time": [int(t) for t in times]}
    for src, dst in var_map.items():
        vals = hourly.get(src)
        if vals is None:
            continue
        unit = units.get(src, "")
        if dst in SPEED_FIELDS and unit:
            vals = [speed_to_kn(v, unit) for v in vals]
        elif dst in LENGTH_FIELDS and unit:
            vals = [length_to_m(v, unit) for v in vals]
        out[dst] = [None if v is None else round(float(v), 2) for v in vals]
    return out


class OpenMeteo:
    def __init__(self, http: Http, *, forecast_url: str, marine_url: str, apikey: str | None = None,
                 models: str = "best_match", forecast_days: int = 5):
        self.http = http
        self.forecast_url = forecast_url.rstrip("/") + "/v1/forecast"
        self.marine_url = marine_url.rstrip("/") + "/v1/marine"
        self.apikey = apikey
        self.models = models
        self.forecast_days = forecast_days

    async def _fetch(self, url: str, points: Sequence[tuple[float, float]], variables: dict[str, str],
                     extra: dict, days: int | None = None) -> list[dict]:
        results: list[dict] = []
        for i in range(0, len(points), MAX_POINTS_PER_REQUEST):
            chunk = points[i:i + MAX_POINTS_PER_REQUEST]
            params = {
                "latitude": ",".join(_fmt(p[0]) for p in chunk),
                "longitude": ",".join(_fmt(p[1]) for p in chunk),
                "hourly": ",".join(variables),
                "timeformat": "unixtime",
                "timezone": "GMT",
                "past_days": 1,
                "forecast_days": days or self.forecast_days,
                **extra,
            }
            if self.apikey:
                params["apikey"] = self.apikey
            resp = await self.http.get(url, params=params)
            if resp.status_code != 200:
                try:
                    reason = resp.json().get("reason")
                except Exception:
                    reason = resp.text[:200]
                raise ProviderError(f"open-meteo HTTP {resp.status_code}: {reason}")
            payload = resp.json()
            objs = payload if isinstance(payload, list) else [payload]
            if len(objs) != len(chunk):
                raise ProviderError(f"open-meteo returned {len(objs)} locations for {len(chunk)} requested")
            results.extend(normalize_location(o, variables) for o in objs)
        return results

    async def forecast(self, points: Sequence[tuple[float, float]], days: int | None = None) -> list[dict]:
        return await self._fetch(self.forecast_url, points, FORECAST_VARS, {
            "wind_speed_unit": "kn",
            "cell_selection": "sea",
            "models": self.models,
        }, days)

    async def marine(self, points: Sequence[tuple[float, float]], days: int | None = None) -> list[dict]:
        return await self._fetch(self.marine_url, points, MARINE_VARS, {
            "length_unit": "metric",
            "cell_selection": "sea",
        }, days)
