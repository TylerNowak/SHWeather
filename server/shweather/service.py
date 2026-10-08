"""WeatherService: the orchestration layer between providers, storage, sensors and analysis.

Everything the API returns is assembled here from local state (SQLite + SensorHub), so
every endpoint keeps working offline; refresh_* methods update that state when the
network is available.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
import time
from collections import OrderedDict
from collections.abc import Callable

import httpx

from . import series as S
from .analysis import beaufort, conditions, nowcast, pressure, zambretti
from .config import BandwidthConfig, BoatProfile, Settings
from .db import Database
from .geo import (
    BBox,
    bbox_of,
    compass_point,
    distance_nm,
    grid_for_radius,
    idw_weights,
    make_grid,
    weighted_direction,
    weighted_scalar,
)
from .imagery import Imagery
from .net import BudgetExceeded, Http
from .providers import NotApplicable, ProviderError
from .providers.coops import Coops, nearest_station
from .providers.ndbc import NDBC
from .providers.ndbc import nearest as nearest_buoys
from .providers.nws import NWS
from .providers.open_meteo import OpenMeteo
from .sensors.hub import SensorHub

log = logging.getLogger(__name__)

NETWORK_ERRORS = (httpx.HTTPError, ProviderError, BudgetExceeded, ValueError)
NWS_FILL_RADIUS_NM = 20.0

# Data-saver mode (bandwidth.saver): smaller, shorter, less frequent downloads.
SAVER_GRID_STEPS = 1            # 3x3 grid instead of 5x5
SAVER_FORECAST_DAYS = 3
SAVER_REFRESH_FACTOR = 3        # forecast refreshed 3x less often
SAVER_JOB_FACTOR = {"alerts": 1.5, "buoys": 4, "marine_text": 3, "tides": 2}

# Positions phones send while they poll their own GPS (every few seconds to an hour) are
# kept in memory and written to the database like the boat's GPS: on the minute flush, once
# the boat has moved or ten minutes have passed, so a busy phone never wears the SD card.
LIVE_SOURCES = {"phone"}
LIVE_SAVE_MOVE_NM = 0.05
LIVE_SAVE_INTERVAL_S = 600
STORED_POSITION_MAX_AGE_S = 7 * 86400


class NoPosition(RuntimeError):
    pass


class WeatherService:
    def __init__(self, settings: Settings, *, db: Database | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.clock = clock
        self.db = db or Database(settings.data_dir / "shweather.db")
        self.http = Http(settings, self.db, transport, limits=self.bandwidth)
        self.hub = SensorHub(settings, clock)
        src = settings.sources
        self.open_meteo = OpenMeteo(self.http, forecast_url=src.open_meteo_url, marine_url=src.open_meteo_marine_url,
                                    apikey=src.open_meteo_apikey, models=settings.forecast.models,
                                    forecast_days=settings.forecast.forecast_days)
        self.nws = NWS(self.http, src.nws_url)
        self.ndbc = NDBC(self.http, src.ndbc_url)
        self.coops = Coops(self.http, src.coops_url)
        cached = self.db.kv_get("source_status")
        self.status: dict[str, dict] = cached[0] if cached else {}
        self._lock = asyncio.Lock()
        self._points_cache: OrderedDict[int, list] = OrderedDict()
        self._interp_cache: dict[tuple, tuple[float, dict]] = {}
        self.imagery = Imagery(settings, self.http, self.db, position=self.position,
                               saver=lambda: self.bandwidth().saver, clock=clock)
        self.live_fix: dict | None = None            # latest fix from a phone's GPS
        self._saved_live: tuple[float, float, float] | None = None
        self.https: dict | None = None               # the HTTPS listener's state, for /api/status

    async def aclose(self) -> None:
        await self.http.aclose()
        self.db.close()  # release the file promptly (Windows won't delete or replace an open file)

    # ------------------------------------------------------------------ status

    def _mark(self, name: str, ok: bool, detail: str | None = None, applicable: bool = True) -> None:
        now = self.clock()
        st = self.status.setdefault(name, {})
        st["last_attempt"] = now
        st["applicable"] = applicable
        if ok:
            st["last_success"] = now
            st.pop("error", None)
            st["failures"] = 0
        elif applicable:
            st["error"] = detail
            st["failures"] = st.get("failures", 0) + 1
        else:
            st["note"] = detail
            st.pop("error", None)
        self.db.kv_set("source_status", self.status)

    def online(self) -> bool:
        """True if any remote source succeeded recently."""
        recent = [s.get("last_success", 0) for s in self.status.values()]
        return bool(recent) and self.clock() - max(recent) < 2 * 3600

    # ------------------------------------------------------------------ boat profile

    def boat(self) -> BoatProfile:
        override = self.db.kv_get("boat_profile")
        if override:
            return BoatProfile.model_validate({**self.settings.boat.model_dump(), **override[0]})
        return self.settings.boat

    def set_boat(self, profile: BoatProfile) -> BoatProfile:
        self.db.kv_set("boat_profile", profile.model_dump())
        self._interp_cache.clear()
        return profile

    # ------------------------------------------------------------------ bandwidth

    def bandwidth(self) -> BandwidthConfig:
        override = self.db.kv_get("bandwidth")
        if override:
            return BandwidthConfig.model_validate({**self.settings.bandwidth.model_dump(), **override[0]})
        return self.settings.bandwidth

    def set_bandwidth(self, cfg: BandwidthConfig) -> BandwidthConfig:
        self.db.kv_set("bandwidth", cfg.model_dump())
        return cfg

    def bandwidth_view(self) -> dict:
        return {"settings": self.bandwidth().model_dump(), "usage": self.http.usage(),
                "history": self.db.net_history(31)}

    def refresh_interval_s(self) -> float:
        base = self.settings.forecast.refresh_minutes * 60
        return base * SAVER_REFRESH_FACTOR if self.bandwidth().saver else base

    # ------------------------------------------------------------------ position

    def position(self) -> dict:
        """Where the boat is: its own GPS, else the newest fix a phone sent or someone typed in
        (up to a week old), else ``home`` from the config."""
        gps = self.hub.position()
        if gps:
            return {**gps, "source": "gps", "age_s": round(self.clock() - gps["ts"], 1)}
        stored = [p for p in (self.live_fix, self.db.last_position()) if p]
        best = max(stored, key=lambda p: p["ts"], default=None)
        if best and self.clock() - best["ts"] < STORED_POSITION_MAX_AGE_S:
            return {**best, "age_s": round(self.clock() - best["ts"], 1)}
        if self.settings.home:
            return {"lat": self.settings.home.lat, "lon": self.settings.home.lon, "source": "home",
                    "ts": None, "age_s": None}
        raise NoPosition("No GPS fix, no stored position and no 'home' in config.yaml")

    def set_position(self, lat: float, lon: float, source: str = "manual",
                     accuracy_m: float | None = None) -> dict:
        """A position from a phone's GPS (``source="phone"``, sent again and again while the
        app is open) or typed in by hand. The boat's own GPS still takes priority."""
        now = self.clock()
        if source in LIVE_SOURCES:
            self.live_fix = {"ts": now, "lat": lat, "lon": lon, "source": source,
                             "accuracy_m": None if accuracy_m is None else round(accuracy_m, 1)}
        else:
            self.live_fix = None   # a position typed in replaces whatever a phone sent before
            self.db.add_position(now, lat, lon, source)
        return self.position()

    def live_fix_to_save(self) -> dict | None:
        """The phone fix to write to the database now, if any (called once a minute)."""
        fix = self.live_fix
        if not fix:
            return None
        last = self._saved_live
        if last is not None and fix["ts"] == last[2]:
            return None
        if (last is None or fix["ts"] - last[2] >= LIVE_SAVE_INTERVAL_S
                or distance_nm(last[0], last[1], fix["lat"], fix["lon"]) >= LIVE_SAVE_MOVE_NM):
            self._saved_live = (fix["lat"], fix["lon"], fix["ts"])
            return fix
        return None

    def save_positions(self) -> None:
        """Write the boat's GPS and the phones' latest fix to the database, sparingly."""
        pos = self.hub.position_to_save()
        if pos:
            self.db.add_position(pos["ts"], pos["lat"], pos["lon"], "gps")
        fix = self.live_fix_to_save()
        if fix:
            self.db.add_position(fix["ts"], fix["lat"], fix["lon"], fix["source"])

    # ------------------------------------------------------------------ refresh: forecast grid

    def latest_run(self) -> dict | None:
        runs = self.db.runs(1)
        return runs[0] if runs else None

    def forecast_due(self) -> tuple[bool, str]:
        try:
            pos = self.position()
        except NoPosition:
            return False, "no position"
        run = self.latest_run()
        if not run:
            return True, "no forecast yet"
        age = self.clock() - run["fetched_at"]
        if age >= self.refresh_interval_s():
            return True, f"forecast is {age / 3600:.1f} h old"
        box = BBox(run["min_lat"], run["max_lat"], run["min_lon"], run["max_lon"])
        if not box.contains(pos["lat"], pos["lon"]):
            return True, "boat has left the forecast grid"
        if run["kind"] == "auto" and distance_nm(pos["lat"], pos["lon"], run["center_lat"], run["center_lon"]) \
                > self.settings.forecast.refetch_distance_nm:
            return True, "boat has moved away from the grid centre"
        if box.edge_distance_deg(pos["lat"], pos["lon"]) < run["spacing_deg"] * 0.5:
            return True, "boat is near the grid edge"
        return False, "fresh"

    async def refresh_forecast(self, radius_nm: float | None = None) -> dict:
        pos = self.position()
        lat, lon = pos["lat"], pos["lon"]
        saver = self.bandwidth().saver
        days = min(self.settings.forecast.forecast_days, SAVER_FORECAST_DAYS) if saver else None
        if radius_nm:
            points, spacing = grid_for_radius(lat, lon, radius_nm, self.settings.forecast.passage_max_points,
                                              self.settings.forecast.grid.spacing_deg)
            kind = "passage"
        else:
            g = self.settings.forecast.grid
            steps = min(g.steps, SAVER_GRID_STEPS) if saver else g.steps
            points, spacing = make_grid(lat, lon, steps, g.spacing_deg), g.spacing_deg
            kind = "auto"
        async with self._lock:
            try:
                fc = await self.open_meteo.forecast(points, days)
                self._mark("open_meteo", True)
            except NETWORK_ERRORS as exc:
                self._mark("open_meteo", False, str(exc))
                raise
            sources = ["open_meteo"]
            try:
                marine = await self.open_meteo.marine(points, days)
                for f, m in zip(fc, marine, strict=True):
                    S.merge(f, m, S.MARINE_FIELDS)
                self._mark("open_meteo_marine", True)
                if any(S.has_data(m, "wave_height_m") for m in marine):
                    sources.append("open_meteo_marine")
            except NETWORK_ERRORS as exc:
                self._mark("open_meteo_marine", False, str(exc))
            run_id = self.db.save_run(kind=kind, center=(lat, lon), bbox=bbox_of(points, lon), spacing_deg=spacing,
                                      model=self.settings.forecast.models,
                                      points=[(p[0], p[1], s) for p, s in zip(points, fc, strict=True)],
                                      sources=sources, fetched_at=self.clock())
            self.db.prune_runs()
            self._interp_cache.clear()
        # The NWS gridpoint is a large document; in saver mode fetch it only when the ocean wave
        # models have nothing here (Great Lakes, inland lakes) and NWS waves are the only waves.
        if self.settings.sources.nws and (not saver or "open_meteo_marine" not in sources):
            await self.refresh_nws_grid(lat, lon)
        return {"run_id": run_id, "kind": kind, "points": len(points), "spacing_deg": spacing, "sources": sources}

    async def _nws_point(self, lat: float, lon: float) -> dict:
        key = f"nws_point:{lat:.2f},{lon:.2f}"
        cached = self.db.kv_get(key, max_age_s=30 * 86400)
        if cached:
            if cached[0].get("not_applicable"):
                raise NotApplicable("outside NWS coverage (cached)")
            return cached[0]
        try:
            info = await self.nws.point(lat, lon)
        except NotApplicable:
            self.db.kv_set(key, {"not_applicable": True})
            raise
        self.db.kv_set(key, info)
        return info

    async def refresh_nws_grid(self, lat: float, lon: float) -> None:
        try:
            info = await self._nws_point(lat, lon)
            grid = await self.nws.gridpoint(info["grid_url"])
            self.db.kv_set("nws_grid", {"lat": lat, "lon": lon, "series": grid, "office": info.get("grid_id")})
            self._interp_cache.clear()
            self._mark("nws_grid", True)
        except NotApplicable as exc:
            self._mark("nws_grid", False, str(exc), applicable=False)
        except NETWORK_ERRORS as exc:
            self._mark("nws_grid", False, str(exc))

    # ------------------------------------------------------------------ refresh: other sources

    async def refresh_alerts(self) -> None:
        if not self.settings.sources.nws:
            return
        pos = self.position()
        try:
            alerts = await self.nws.alerts(pos["lat"], pos["lon"])
            self.db.kv_set("alerts", {"lat": pos["lat"], "lon": pos["lon"], "alerts": alerts})
            self._mark("nws_alerts", True)
        except NotApplicable as exc:
            self.db.kv_set("alerts", {"lat": pos["lat"], "lon": pos["lon"], "alerts": []})
            self._mark("nws_alerts", False, str(exc), applicable=False)
        except NETWORK_ERRORS as exc:
            self._mark("nws_alerts", False, str(exc))

    async def refresh_marine_text(self) -> None:
        if not self.settings.sources.nws:
            return
        pos = self.position()
        try:
            zone_id = self.settings.sources.marine_zone
            offices: list[str] = []
            if zone_id:
                offices = await self.nws.zone_offices(zone_id)
            else:
                zone = await self.nws.marine_zone(pos["lat"], pos["lon"])
                if not zone:
                    raise NotApplicable("position is not inside an NWS marine zone")
                zone_id, offices = zone["id"], zone["offices"]
            if not offices:
                try:
                    offices = [(await self._nws_point(pos["lat"], pos["lon"]))["cwa"]]
                except NotApplicable:
                    offices = []
            text = await self.nws.marine_text(zone_id, offices)
            self.db.kv_set("marine_text", text)
            self._mark("nws_marine_text", True)
        except NotApplicable as exc:
            self._mark("nws_marine_text", False, str(exc), applicable=False)
        except NETWORK_ERRORS as exc:
            self._mark("nws_marine_text", False, str(exc))

    async def refresh_buoys(self) -> None:
        if not self.settings.sources.ndbc:
            return
        pos = self.position()
        try:
            stations = await self.ndbc.latest_all()
            near = nearest_buoys(stations, pos["lat"], pos["lon"], self.settings.sources.ndbc_radius_nm,
                                 now=self.clock())
            self.db.kv_set("buoys", {"lat": pos["lat"], "lon": pos["lon"], "stations": near})
            if near:
                self._mark("ndbc", True)
            else:
                radius = self.settings.sources.ndbc_radius_nm
                self._mark("ndbc", False, f"no reporting station within {radius:.0f} nm", applicable=False)
        except NETWORK_ERRORS as exc:
            self._mark("ndbc", False, str(exc))

    async def _coops_stations(self, kind: str) -> list[dict]:
        key = f"coops_stations:{kind}"
        cached = self.db.kv_get(key, max_age_s=30 * 86400)
        if cached:
            return cached[0]
        stations = await self.coops.stations(kind)
        self.db.kv_set(key, stations)
        return stations

    async def refresh_tides(self) -> None:
        if not self.settings.sources.coops:
            return
        pos = self.position()
        radius = self.settings.sources.coops_radius_nm
        start = self.clock() - 6 * 3600
        out: dict = {"lat": pos["lat"], "lon": pos["lon"], "tide": None, "current": None}
        any_ok, errors = False, []
        try:
            st = nearest_station(await self._coops_stations("tidepredictions"), pos["lat"], pos["lon"], radius)
            out["tide"] = {"station": st, "hilo": await self.coops.tide_hilo(st["id"], start, 78),
                           "curve": await self.coops.tide_curve(st["id"], start, 54)}
            any_ok = True
        except NotApplicable as exc:
            errors.append(str(exc))
        except NETWORK_ERRORS as exc:
            self._mark("coops", False, str(exc))
            return
        try:
            st = nearest_station(await self._coops_stations("currentpredictions"), pos["lat"], pos["lon"], radius)
            out["current"] = {"station": st, "events": await self.coops.currents(st["id"], start, 54)}
            any_ok = True
        except NotApplicable as exc:
            errors.append(str(exc))
        except NETWORK_ERRORS as exc:
            log.info("tidal currents unavailable: %s", exc)
        self.db.kv_set("tides", out)
        if any_ok:
            self._mark("coops", True)
        else:
            self._mark("coops", False, f"no tide or tidal-current station within {radius:.0f} nm", applicable=False)

    async def refresh_all(self, radius_nm: float | None = None) -> dict:
        result: dict = {}
        try:
            result["forecast"] = await self.refresh_forecast(radius_nm)
        except NETWORK_ERRORS as exc:
            result["forecast"] = {"error": str(exc)}
        await asyncio.gather(self.refresh_alerts(), self.refresh_marine_text(), self.refresh_buoys(),
                             self.refresh_tides())
        result["status"] = self.status
        return result

    # ------------------------------------------------------------------ forecast at a position

    def _points(self, run_id: int) -> list:
        if run_id in self._points_cache:
            self._points_cache.move_to_end(run_id)
            return self._points_cache[run_id]
        pts = self.db.run_points(run_id)
        self._points_cache[run_id] = pts
        while len(self._points_cache) > 3:
            self._points_cache.popitem(last=False)
        return pts

    def _select_run(self, lat: float, lon: float) -> tuple[dict, bool] | None:
        runs = self.db.runs(20)
        for r in runs:  # newest first
            box = BBox(r["min_lat"], r["max_lat"], r["min_lon"], r["max_lon"])
            if box.contains(lat, lon, margin_deg=r["spacing_deg"] * 0.5):
                return r, True
        for r in runs:
            if distance_nm(lat, lon, r["center_lat"], r["center_lon"]) < 120:
                return r, False
        return None

    @staticmethod
    def interpolate(points: list, lat: float, lon: float) -> dict:
        coords = [(p[0], p[1]) for p in points]
        weights = idw_weights(lat, lon, coords)
        chosen = [points[i][2] for i, _ in weights]
        w = [wt for _, wt in weights]
        n = min(len(s["time"]) for s in chosen)
        out: dict = {"time": chosen[0]["time"][:n]}
        fields = sorted({k for s in chosen for k in s if k != "time"})
        for f in fields:
            cols = [s.get(f) or [None] * n for s in chosen]
            if f in S.DIRECTION_FIELDS:
                mag = S.DIRECTION_MAGNITUDE.get(f)
                mcols = [s.get(mag) or [None] * n for s in chosen] if mag else None
                out[f] = [None if (d := weighted_direction([c[i] for c in cols], w,
                                                              [m[i] for m in mcols] if mcols else None)) is None
                          else round(d) for i in range(n)]
            elif f in ("weather_code", "is_day"):
                # categorical: take the value from the nearest point
                out[f] = cols[0][:n]
            else:
                out[f] = [None if (v := weighted_scalar([c[i] for c in cols], w)) is None else round(v, 2)
                          for i in range(n)]
        return out

    def forecast_at(self, lat: float, lon: float, *, localize: bool = True) -> dict | None:
        sel = self._select_run(lat, lon)
        if not sel:
            return None
        run, inside = sel
        key = (run["id"], round(lat, 3), round(lon, 3), localize)
        now = self.clock()
        hit = self._interp_cache.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
        series = self.interpolate(self._points(run["id"]), lat, lon)
        filled = []
        nws = self.db.kv_get("nws_grid")
        if nws and distance_nm(lat, lon, nws[0]["lat"], nws[0]["lon"]) <= NWS_FILL_RADIUS_NM:
            ns = nws[0]["series"]
            for f in S.MARINE_FIELDS:
                if not S.has_data(series, f) and S.has_data(ns, f):
                    S.merge(series, ns, [f])
                    filled.append(f)
        bias = None
        raw = {k: list(series.get(k) or []) for k in ("wind_speed_kn", "wind_dir_deg", "pressure_hpa")}
        if localize:
            since = now - (nowcast.LOOKBACK_H + 1) * 3600
            obs = {m: self.db.observations(m, since) for m in ("tws_kn", "twd_deg", "pressure_hpa")}
            series, bias = nowcast.localize(series, obs, now)
        result = {
            "lat": lat, "lon": lon,
            "run": {"id": run["id"], "kind": run["kind"], "fetched_at": run["fetched_at"],
                    "age_s": round(now - run["fetched_at"]), "model": run["model"], "spacing_deg": run["spacing_deg"],
                    "inside_grid": inside, "sources": json.loads(run["sources"] or "[]"),
                    "distance_from_center_nm": round(distance_nm(lat, lon, run["center_lat"], run["center_lon"]), 1)},
            "filled_from_nws": filled,
            "local_correction": bias,
            "series": series,
            "raw": raw,
        }
        self._interp_cache[key] = (now, result)
        return result

    # ------------------------------------------------------------------ assembled views

    def pressure_samples(self, hours: float = 3.5) -> list[tuple[float, float]]:
        now = self.clock()
        samples = self.db.observations("pressure_hpa", now - hours * 3600)
        live = self.hub.value("pressure_hpa")
        if live is not None:
            samples.append((now, live))
        return samples

    def now(self) -> dict:
        now = self.clock()
        pos = self.position()
        fc = self.forecast_at(pos["lat"], pos["lon"])
        inst = self.hub.snapshot()
        fc_row: dict = {}
        if fc:
            i = S.index_at(fc["series"], now)
            if i is not None:
                fc_row = S.row(fc["series"], i)

        def pick(inst_key: str, fc_key: str):
            if inst_key in inst:
                return {"value": inst[inst_key]["value"], "source": "instrument", "age_s": inst[inst_key]["age_s"]}
            if fc_row.get(fc_key) is not None:
                return {"value": fc_row[fc_key], "source": "forecast", "age_s": fc["run"]["age_s"] if fc else None}
            return None

        wind = pick("tws_kn", "wind_speed_kn")
        wdir = pick("twd_deg", "wind_dir_deg")
        gust = None
        recent = self.db.observations("tws_max_kn", now - 600)
        if wind and wind["source"] == "instrument" and recent:
            gust = {"value": round(max(v for _, v in recent), 1), "source": "instrument", "window_s": 600}
        elif fc_row.get("wind_gust_kn") is not None:
            gust = {"value": fc_row["wind_gust_kn"], "source": "forecast"}
        baro = pick("pressure_hpa", "pressure_hpa")

        tend = pressure.tendency(self.pressure_samples(), now)
        if tend:
            tend["source"] = "instrument"
        elif fc:
            # Uncorrected model pressure: the local correction shifts only future hours, which
            # would otherwise masquerade as a pressure change.
            raw_p = {"time": fc["series"]["time"], "pressure_hpa": fc["raw"]["pressure_hpa"]}
            p_now = S.value_at(raw_p, "pressure_hpa", now)
            p_then = S.value_at(raw_p, "pressure_hpa", now - 3 * 3600)
            if p_now is not None and p_then is not None:
                ch = round(p_now - p_then, 1)
                tend = {"change_hpa": ch, "estimated": True, **pressure.classify(ch),
                        "warnings": pressure.warnings(ch, p_now), "source": "forecast"}

        zam = None
        if baro and tend:
            month = dt.datetime.fromtimestamp(now, dt.UTC).month
            zam = zambretti.forecast(baro["value"], zambretti.trend_from_change(tend["change_hpa"]), month,
                                     pos["lat"], wdir["value"] if wdir else None)

        combined = dict(fc_row)
        if wind:
            combined["wind_speed_kn"] = wind["value"]
        if gust:
            combined["wind_gust_kn"] = gust["value"]
        boat = self.boat()
        assessment = conditions.assess(combined, boat)

        outlook = None
        if fc:
            nxt = S.window(fc["series"], now, now + 24 * 3600)
            levels = conditions.assess_series(nxt, boat)
            worst_i = max(range(len(levels)), key=lambda i: conditions.LEVELS.index(levels[i]["level"])
                          if levels[i]["level"] in conditions.LEVELS else -1, default=None)

            def peak(field):
                col = [(v, t) for v, t in zip(nxt.get(field) or [], nxt["time"], strict=False) if v is not None]
                return max(col) if col else None

            pw, pg, pv = peak("wind_speed_kn"), peak("wind_gust_kn"), peak("wave_height_m")
            outlook = {
                "worst": None if worst_i is None else {**levels[worst_i], "time": nxt["time"][worst_i]},
                "max_wind": pw and {"value": pw[0], "time": pw[1]},
                "max_gust": pg and {"value": pg[0], "time": pg[1]},
                "max_wave": pv and {"value": pv[0], "time": pv[1]},
            }

        alerts = self.db.kv_get("alerts")
        return {
            "time": now,
            "position": pos,
            "wind": wind and {**wind, "beaufort": beaufort.describe(wind["value"])},
            "wind_dir": wdir and {**wdir, "compass": compass_point(wdir["value"])},
            "gust": gust,
            "pressure": baro,
            "tendency": tend,
            "zambretti": zam,
            "waves": {k: fc_row.get(k) for k in ("wave_height_m", "wave_period_s", "wave_dir_deg",
                                                  "wind_wave_height_m", "swell_height_m", "swell_period_s",
                                                  "swell_dir_deg")} if fc_row else None,
            "sea": {"water_temp_c": pick("water_temp_c", "sst_c"),
                    "current_speed_kn": fc_row.get("current_speed_kn"),
                    "current_dir_deg": fc_row.get("current_dir_deg")},
            "air": {"temp_c": pick("air_temp_c", "temp_c"), "humidity_pct": pick("humidity_pct", "humidity_pct"),
                    "visibility_m": fc_row.get("visibility_m"), "cloud_pct": fc_row.get("cloud_pct"),
                    "precip_mm": fc_row.get("precip_mm"), "weather_code": fc_row.get("weather_code"),
                    "cape_jkg": fc_row.get("cape_jkg")},
            "instruments": inst,
            "assessment": assessment,
            "outlook_24h": outlook,
            "alerts": alerts[0]["alerts"] if alerts else [],
            "alerts_age_s": round(now - alerts[1]) if alerts else None,
            "forecast": fc and {"run": fc["run"], "local_correction": fc["local_correction"],
                                "filled_from_nws": fc["filled_from_nws"]},
            "online": self.online(),
        }

    def forecast_view(self, lat: float | None, lon: float | None, hours: int = 72, past_hours: int = 6) -> dict | None:
        if lat is None or lon is None:
            pos = self.position()
            lat, lon = pos["lat"], pos["lon"]
        fc = self.forecast_at(lat, lon)
        if not fc:
            return None
        now = self.clock()
        start = now - past_hours * 3600 - 3599
        s = S.window(fc["series"], start, now + hours * 3600)
        idx0 = fc["series"]["time"].index(s["time"][0]) if s["time"] else 0
        raw = {k: v[idx0: idx0 + len(s["time"])] for k, v in fc["raw"].items()}
        return {**{k: v for k, v in fc.items() if k not in ("series", "raw")},
                "series": s, "raw": raw, "assessment": conditions.assess_series(s, self.boat())}

    # Fields drawn on the Radar tab's map, and how many decimals they need there.
    MAP_FIELDS = {"wind_speed_kn": 1, "wind_gust_kn": 1, "wind_dir_deg": 0, "cloud_pct": 0, "precip_mm": 1}

    def _map_run(self, lat: float, lon: float) -> dict | None:
        """The forecast grid to draw: the newest one around the boat, or a wider one (a passage
        download) fetched at about the same time, else whatever forecast_at would use."""
        runs = [r for r in self.db.runs(20)
                if BBox(r["min_lat"], r["max_lat"], r["min_lon"], r["max_lon"]).contains(lat, lon)]
        if runs:
            newest = max(r["fetched_at"] for r in runs)
            recent = [r for r in runs if newest - r["fetched_at"] <= 90 * 60]
            return max(recent, key=lambda r: ((r["max_lat"] - r["min_lat"]) * (r["max_lon"] - r["min_lon"]),
                                              r["fetched_at"]))
        sel = self._select_run(lat, lon)
        return sel[0] if sel else None

    def map_fields(self, past_hours: int = 3, hours: int = 48) -> dict | None:
        """Model wind, cloud and rain at every grid point, for the Radar tab's forecast layers."""
        pos = self.position()
        run = self._map_run(pos["lat"], pos["lon"])
        if not run:
            return None
        pts = self._points(run["id"])
        now = self.clock()
        times = pts[0][2]["time"]
        keep = [i for i, t in enumerate(times) if now - past_hours * 3600 - 3599 <= t <= now + hours * 3600]

        def col(series: dict, field: str, digits: int) -> list:
            vals = series.get(field) or []
            return [None if i >= len(vals) or vals[i] is None else round(vals[i], digits) if digits else round(vals[i])
                    for i in keep]

        return {
            "time": [times[i] for i in keep],
            "points": [[la, lo] for la, lo, _ in pts],
            "fields": {f: [col(s, f, d) for _, _, s in pts] for f, d in self.MAP_FIELDS.items()},
            "run": {"id": run["id"], "kind": run["kind"], "fetched_at": run["fetched_at"],
                    "age_s": round(now - run["fetched_at"]), "model": run["model"], "spacing_deg": run["spacing_deg"],
                    "bbox": [run["min_lat"], run["min_lon"], run["max_lat"], run["max_lon"]],
                    "center": [run["center_lat"], run["center_lon"]]},
        }

    def observations(self, metrics: list[str], hours: float) -> dict:
        since = self.clock() - hours * 3600
        return {m: [[round(t), v] for t, v in self.db.observations(m, since)] for m in metrics}

    def cached(self, key: str) -> dict:
        hit = self.db.kv_get(key)
        if not hit:
            return {"available": False, "age_s": None}
        return {"available": True, "age_s": round(self.clock() - hit[1]), "data": hit[0]}

    def status_view(self) -> dict:
        from . import __version__
        run = self.latest_run()
        try:
            pos = self.position()
        except NoPosition:
            pos = None
        due, why = self.forecast_due()
        return {
            "version": __version__,
            "time": self.clock(),
            "demo": self.settings.demo,
            "position": pos,
            "online": self.online(),
            "sources": self.status,
            "sensors": self.hub.sources,
            "forecast": run and {"fetched_at": run["fetched_at"], "age_s": round(self.clock() - run["fetched_at"]),
                                 "kind": run["kind"], "model": run["model"], "spacing_deg": run["spacing_deg"],
                                 "bbox": [run["min_lat"], run["min_lon"], run["max_lat"], run["max_lon"]]},
            "forecast_due": {"due": due, "reason": why},
            "bandwidth": self.http.usage(),
            "display": self.settings.display.model_dump(),
            "platform": sys.platform,
            "https": self.https or {"enabled": False},
        }
