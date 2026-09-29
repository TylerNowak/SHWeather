"""Demo mode: realistic synthetic data flowing through the *real* provider code paths.

``shweather serve --demo`` answers every outbound HTTP request with a synthetic payload in
the exact format of the real service (Open-Meteo, NWS, NDBC, CO-OPS), and simulates boat
instruments. It is used for UI development, screenshots and tests, and lets anyone try
the app without a boat or an internet connection.

The scenario is a cold front passing ~9 h after start: southerly breeze building ahead of
it, a falling barometer, a thunderstorm line at the front, then a gusty north-westerly.
The days after (for the Forecast tab): a clear high with fog one morning, a warm-up in
southerly return flow, then a low with steady rain.
The simulated instruments read ~20% more wind than the "forecast", so the local correction
(nowcast) visibly kicks in.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import math
import random
import re
import time
from urllib.parse import parse_qs

import httpx

from .config import Position, Settings
from .demo_imagery import DemoSky, ir_tile, radar_tile
from .service import WeatherService
from .simulator import BoatSim

HOUR = 3600
DEMO_HOME = Position(lat=41.90, lon=-87.50)  # Lake Michigan, ~6 nm off Chicago
INSTRUMENT_BIAS = {"ratio": 1.2, "dir_offset": 8.0, "pressure_offset": 0.8}


def in_great_lakes(lat: float, lon: float) -> bool:
    """Crude box, demo only."""
    return 41.3 <= lat <= 49.1 and -92.3 <= lon <= -76.0


def in_us(lat: float, lon: float) -> bool:
    return 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0


class Scenario:
    def __init__(self, t0: float, lat0: float, lon0: float, front_in_h: float = 9.0):
        self.tf = t0 + front_in_h * HOUR
        self.lat0, self.lon0 = lat0, lon0

    def at(self, t: float, lat: float, lon: float) -> dict:
        # Hours relative to frontal passage; the front reaches eastern points later.
        x = (t - self.tf) / HOUR - (lon - self.lon0) * 2.0
        sig = 1.0 / (1.0 + math.exp(-x / 1.5))
        local_h = ((t / HOUR) + lon / 15.0) % 24
        diurnal = math.sin(2 * math.pi * (local_h - 10) / 24)
        # The days after the front, for the Forecast tab: high pressure and clear skies
        # (with fog one morning), a warm-up in southerly return flow, then a low with steady rain.
        high = math.exp(-((x - 36) / 14) ** 2)
        warm = math.exp(-((x - 70) / 16) ** 2)
        low = math.exp(-((x - 86) / 7) ** 2)
        speed = max(0.5, 9 + 13 * math.exp(-(x / 5) ** 2) + 7 * sig * math.exp(-max(x, 0) / 16)
                    + 2 * diurnal + (lat - self.lat0) * 4 - 4 * high + 7 * low)
        direction = (210 + 95 * sig - 125 * math.exp(-((x - 80) / 14) ** 2) + 5 * math.sin(t / HOUR / 3)) % 360
        gust = speed * (1.3 + 0.25 * math.exp(-(x / 2) ** 2))
        pressure = (1016 + 0.06 * x - 11 * math.exp(-(x / 7) ** 2) + (lat - self.lat0) * 1.5
                    + 7 * high - 9 * low)
        temp = 21 - 8 * sig + 6 * warm + (3 + 2 * high) * math.sin(2 * math.pi * (local_h - 9) / 24)
        dew = 15 - 9 * sig + 6 * warm
        rh = 100 * math.exp(17.625 * dew / (243.04 + dew)) / math.exp(17.625 * temp / (243.04 + temp))
        precip = 3.5 * math.exp(-(x / 1.2) ** 2) + 0.4 * math.exp(-((x - 4) / 3) ** 2) + 2.2 * low
        cloud = max(0.0, min(100.0, 25 + 70 * math.exp(-(x / 5) ** 2) - 25 * high
                             + 75 * math.exp(-((x - 84) / 10) ** 2) + 15 * math.sin(x / 5) * warm))
        cape = 1400 * math.exp(-((x + 1) / 3) ** 2)
        fog = 26 < x < 50 and 4 <= local_h < 9
        vis = 400.0 if fog else 24000 - 18000 * math.exp(-(x / 1.5) ** 2) - 14000 * low
        if abs(x) < 1:
            code = 95
        elif x > 40 and precip > 0.1:
            code = 63 if precip >= 1 else 61
        elif precip > 0.5:
            code = 80
        elif precip > 0.1:
            code = 61
        elif fog:
            code = 45
        else:
            code = 3 if cloud > 80 else 2 if cloud > 45 else 1 if cloud > 15 else 0
        lake = in_great_lakes(lat, lon)
        wave = (0.2 + 0.0045 * speed ** 1.8) if lake else (0.3 + 0.0065 * speed ** 1.8)
        period = (2 + 0.17 * speed) if lake else (3 + 0.22 * speed)
        return {
            "wind": speed, "dir": direction, "gust": gust, "pressure": pressure, "temp": temp, "dew": dew,
            "rh": min(100.0, rh), "precip": precip,
            "precip_prob": min(100.0, 10 + 90 * math.exp(-(x / 3) ** 2) + 80 * math.exp(-((x - 85) / 9) ** 2)),
            "cloud": cloud, "cape": cape, "vis": vis, "code": code, "is_day": 1 if 6 <= local_h < 19 else 0,
            "wave": wave, "period": period, "wave_dir": direction,
            "swell": None if lake else 0.8, "swell_period": None if lake else 11.0, "swell_dir": None if lake else 250.0,
            "current": None if lake else 0.4 * 1.852, "current_dir": None if lake else 30.0,
            "sst": 17.0 if lake else 19.5,
        }


def _times(past_days: int, forecast_days: int, now: float) -> list[int]:
    day0 = int(now // 86400 * 86400) - past_days * 86400
    return [day0 + i * HOUR for i in range((past_days + forecast_days) * 24)]


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")


class DemoHandler:
    GIBS_LATENCY_S = 1800   # like the real service, new satellite images appear some time later
    GIBS_LEVEL = 6

    def __init__(self, scenario: Scenario, home: Position, clock=time.time):
        self.sc = scenario
        self.home = home
        self.clock = clock
        self.sky = DemoSky(clock(), scenario.lat0, scenario.lon0, scenario.tf)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        try:
            if "marine-api" in host:
                return self._open_meteo(q, marine=True)
            if "open-meteo" in host:
                return self._open_meteo(q, marine=False)
            if "weather.gov" in host:
                return self._nws(path, q)
            if "ndbc" in host:
                return self._ndbc(path)
            if "tidesandcurrents" in host:
                return self._coops(path, q)
            if "rainviewer" in host:
                return self._rainviewer(host, path)
            if "gibs" in host:
                return self._gibs(path)
        except Exception as exc:  # pragma: no cover - demo robustness
            return httpx.Response(500, json={"error": str(exc)})
        return httpx.Response(404, text="demo: unknown host")

    # ---- radar and satellite imagery ----
    def _rainviewer(self, host: str, path: str) -> httpx.Response:
        if path.endswith("/public/weather-maps.json"):
            last = int(self.clock() // 600 * 600)
            frames = [{"time": t, "path": f"/v2/radar/{t}"} for t in range(last - 12 * 600, last + 1, 600)]
            return httpx.Response(200, json={"version": "2.0", "generated": last + 30,
                                             "host": "https://tilecache.rainviewer.com",
                                             "radar": {"past": frames, "nowcast": []}, "satellite": {"infrared": []}})
        m = re.fullmatch(r"/v2/radar/(\d+)/256/(\d+)/(\d+)/(\d+)/2/1_1\.png", path)
        if not m or int(m[2]) > 7:
            return httpx.Response(404, text="demo")
        t, z, x, y = (int(g) for g in m.groups())
        return self._png(radar_tile, t, z, x, y)

    def _gibs(self, path: str) -> httpx.Response:
        m = re.fullmatch(r"/wmts/epsg3857/best/[\w-]+/default/([\w:-]+)/GoogleMapsCompatible_Level(\d+)/(\d+)/(\d+)/(\d+)\.png",
                         path)
        if not m or int(m[2]) != self.GIBS_LEVEL or int(m[3]) > self.GIBS_LEVEL:
            return httpx.Response(400, text="<ExceptionReport>demo: bad request</ExceptionReport>")
        stamp, z, y, x = m[1], int(m[3]), int(m[4]), int(m[5])
        latest = (self.clock() - self.GIBS_LATENCY_S) // 600 * 600
        if stamp == "default":
            t = latest
        else:
            t = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
            if t > latest or t % 600:
                return httpx.Response(400, text="<ExceptionReport>demo: no image for this time</ExceptionReport>")
        return self._png(ir_tile, t, z, x, y)

    def _png(self, render, t: int, z: int, x: int, y: int):
        """Draw a synthetic tile in a worker thread, so the demo server stays responsive.
        (httpx's MockTransport awaits a handler that returns a coroutine.)"""
        async def respond() -> httpx.Response:
            data = await asyncio.to_thread(render, self.sky, t, z, x, y)
            return httpx.Response(200, content=data, headers={"content-type": "image/png"})
        return respond()

    # ---- Open-Meteo ----
    def _open_meteo(self, q: dict, marine: bool) -> httpx.Response:
        lats = [float(x) for x in q["latitude"].split(",")]
        lons = [float(x) for x in q["longitude"].split(",")]
        times = _times(int(q.get("past_days", 0)), int(q.get("forecast_days", 7)), self.clock())
        kn = q.get("wind_speed_unit") == "kn"
        out = []
        for la, lo in zip(lats, lons, strict=True):
            rows = [self.sc.at(t, la, lo) for t in times]
            if marine:
                lake = in_great_lakes(la, lo)
                def col(k, rows=rows, lake=lake):
                    return [None if lake or r[k] is None else round(r[k], 2) for r in rows]
                hourly = {"time": times, "wave_height": col("wave"), "wave_direction": col("wave_dir"),
                          "wave_period": col("period"), "wind_wave_height": col("wave"),
                          "wind_wave_direction": col("wave_dir"), "wind_wave_period": col("period"),
                          "swell_wave_height": col("swell"), "swell_wave_direction": col("swell_dir"),
                          "swell_wave_period": col("swell_period"), "ocean_current_velocity": col("current"),
                          "ocean_current_direction": col("current_dir"), "sea_surface_temperature": col("sst")}
                units = {"time": "unixtime", "wave_height": "m", "wave_direction": "°", "wave_period": "s",
                         "wind_wave_height": "m", "wind_wave_direction": "°", "wind_wave_period": "s",
                         "swell_wave_height": "m", "swell_wave_direction": "°", "swell_wave_period": "s",
                         "ocean_current_velocity": "km/h", "ocean_current_direction": "°",
                         "sea_surface_temperature": "°C"}
            else:
                f = 1.0 if kn else 1.852
                hourly = {"time": times,
                          "wind_speed_10m": [round(r["wind"] * f, 1) for r in rows],
                          "wind_direction_10m": [round(r["dir"]) for r in rows],
                          "wind_gusts_10m": [round(r["gust"] * f, 1) for r in rows],
                          "pressure_msl": [round(r["pressure"], 1) for r in rows],
                          "temperature_2m": [round(r["temp"], 1) for r in rows],
                          "dew_point_2m": [round(r["dew"], 1) for r in rows],
                          "relative_humidity_2m": [round(r["rh"]) for r in rows],
                          "precipitation": [round(r["precip"], 1) for r in rows],
                          "precipitation_probability": [round(r["precip_prob"]) for r in rows],
                          "cloud_cover": [round(r["cloud"]) for r in rows],
                          "visibility": [round(r["vis"]) for r in rows],
                          "cape": [round(r["cape"]) for r in rows],
                          "weather_code": [r["code"] for r in rows],
                          "is_day": [r["is_day"] for r in rows]}
                units = {"time": "unixtime", "wind_speed_10m": "kn" if kn else "km/h", "wind_direction_10m": "°",
                         "wind_gusts_10m": "kn" if kn else "km/h", "pressure_msl": "hPa", "temperature_2m": "°C",
                         "dew_point_2m": "°C", "relative_humidity_2m": "%", "precipitation": "mm",
                         "precipitation_probability": "%", "cloud_cover": "%", "visibility": "m", "cape": "J/kg",
                         "weather_code": "wmo code", "is_day": ""}
            out.append({"latitude": la, "longitude": lo, "generationtime_ms": 0.4, "utc_offset_seconds": 0,
                        "timezone": "GMT", "timezone_abbreviation": "GMT", "elevation": 176.0,
                        "hourly_units": units, "hourly": hourly})
        return httpx.Response(200, json=out if len(out) > 1 else out[0])

    # ---- NWS ----
    def _nws(self, path: str, q: dict) -> httpx.Response:
        now = self.clock()
        lake = in_great_lakes(self.home.lat, self.home.lon)
        if m := re.match(r"^/points/([-\d.]+),([-\d.]+)$", path):
            la, lo = float(m.group(1)), float(m.group(2))
            if not in_us(la, lo):
                return httpx.Response(404, json={"title": "Not Found", "detail": "Unable to provide data for requested point"})
            return httpx.Response(200, json={"properties": {
                "gridId": "LOT", "gridX": 76, "gridY": 73, "cwa": "LOT", "timeZone": "America/Chicago",
                "forecastGridData": "https://api.weather.gov/gridpoints/LOT/76,73",
                "forecastZone": "https://api.weather.gov/zones/forecast/ILZ014"}})
        if path.startswith("/gridpoints/"):
            t0 = int(now // HOUR * HOUR) - 6 * HOUR
            ts = [t0 + i * HOUR for i in range(7 * 24)]
            rows = [(t, self.sc.at(t, self.home.lat, self.home.lon)) for t in ts]

            def layer(uom, fn, step=1):
                return {"uom": uom, "values": [{"validTime": f"{_iso(t)}/PT{step}H", "value": fn(r)}
                                               for t, r in rows[::step]]}
            props = {
                "updateTime": _iso(now - 1800),
                "windSpeed": layer("wmoUnit:km_h-1", lambda r: round(r["wind"] * 1.852 * 0.95, 1)),
                "windGust": layer("wmoUnit:km_h-1", lambda r: round(r["gust"] * 1.852, 1)),
                "windDirection": layer("wmoUnit:degree_(angle)", lambda r: round(r["dir"])),
                "waveHeight": layer("wmoUnit:m", lambda r: round(r["wave"], 2), step=3),
                "wavePeriod": layer("nwsUnit:s", lambda r: round(r["period"]), step=3),
                "waveDirection": layer("wmoUnit:degree_(angle)", lambda r: round(r["wave_dir"]), step=3),
            }
            return httpx.Response(200, json={"type": "Feature", "properties": props})
        if path == "/alerts/active":
            la, lo = (float(x) for x in q["point"].split(","))
            feats = []
            if in_us(la, lo):
                feats.append({"id": "demo-sca", "properties": {
                    "id": "demo-sca", "event": "Small Craft Advisory", "severity": "Minor", "urgency": "Expected",
                    "certainty": "Likely", "onset": _iso(now + 5 * HOUR), "ends": _iso(now + 30 * HOUR),
                    "areaDesc": "Demo nearshore waters", "senderName": "NWS Demo Office",
                    "headline": "Small Craft Advisory from this evening through tomorrow afternoon (DEMO)",
                    "description": "* WHAT...Southwest winds 15 to 25 kt with gusts up to 30 kt, becoming "
                                   "northwest behind a cold front. Waves 3 to 6 ft.\n\n* WHERE...Demo waters.\n\n"
                                   "* IMPACTS...Conditions will be hazardous to small craft.\n\n"
                                   "THIS IS DEMO DATA, NOT A REAL ALERT.",
                    "instruction": "Inexperienced mariners, especially those operating smaller vessels, "
                                   "should avoid navigating in hazardous conditions."}})
            return httpx.Response(200, json={"type": "FeatureCollection", "features": feats})
        if path == "/zones":
            zone = ({"id": "LMZ741", "type": "marine", "name": "Wilmette Harbor to Northerly Island IL", "cwa": ["LOT"]}
                    if lake else {"id": "ANZ330", "type": "marine", "name": "Demo Coastal Waters", "cwa": ["BOX"]})
            return httpx.Response(200, json={"features": [{"properties": zone},
                                                          {"properties": {"id": "ILZ014", "type": "public"}}]})
        if m := re.match(r"^/products/types/(\w+)/locations/(\w+)$", path):
            ptype = m.group(1)
            if ptype not in ("NSH", "CWF"):
                return httpx.Response(200, json={"@graph": []})
            pid = f"demo-{ptype.lower()}"
            return httpx.Response(200, json={"@graph": [{"@id": f"https://api.weather.gov/products/{pid}",
                                                         "id": pid, "issuanceTime": _iso(now - 3600)}]})
        if path.startswith("/products/demo-"):
            zone = "LMZ741" if lake else "ANZ330"
            prefix, num = zone[:3], int(zone[3:])
            text = (f"000\nFZUS53 KLOT 281434\nNSHLOT\n\nNearshore Marine Forecast\nNational Weather Service Demo\n\n"
                    f"{prefix}{num - 1:03d}>{num + 1:03d}-282145-\nDemo nearshore waters-\n\n"
                    "** DEMO DATA - NOT A REAL FORECAST **\n\n"
                    "...SMALL CRAFT ADVISORY IN EFFECT FROM THIS EVENING THROUGH\nTOMORROW AFTERNOON...\n\n"
                    ".TODAY...South winds 10 to 15 kt. Waves 1 to 3 ft.\n"
                    ".TONIGHT...Southwest winds 15 to 25 kt with gusts to 30 kt. Chance of\nthunderstorms. "
                    "Waves 3 to 5 ft.\n"
                    ".TOMORROW...Northwest winds 20 to 25 kt with gusts to 30 kt. Waves 4\nto 6 ft.\n"
                    ".TOMORROW NIGHT...Northwest winds 10 to 20 kt diminishing to 10 to\n15 kt. "
                    "Waves 3 to 5 ft subsiding to 2 to 4 ft.\n\n$$\n\n"
                    f"{prefix}{num + 5:03d}-282145-\nOther demo waters-\n\n.TODAY...Not your zone.\n\n$$\n")
            return httpx.Response(200, json={"productText": text, "issuanceTime": _iso(now - 3600)})
        return httpx.Response(404, json={"title": "Not Found", "detail": f"demo: {path}"})

    # ---- NDBC ----
    def _ndbc(self, path: str) -> httpx.Response:
        if not path.endswith("latest_obs.txt"):
            return httpx.Response(404, text="demo")
        now = self.clock()
        t = now - (now % 600) - 600
        g = time.gmtime(t)
        lines = ["#STN       LAT      LON  YYYY MM DD hh mm WDIR WSPD   GST WVHT  DPD APD MWD   PRES  PTDY  ATMP  WTMP  DEWP  VIS   TIDE",
                 "#text      deg      deg   yr mo day hr mn degT  m/s   m/s   m   sec sec degT   hPa   hPa  degC  degC  degC  nmi     ft"]
        for sid, dla, dlo in (("DMO01", 0.18, 0.10), ("DMO02", -0.25, 0.05), ("DMO03", 0.40, -0.12)):
            la, lo = self.home.lat + dla, self.home.lon + dlo
            r = self.sc.at(t, la, lo)
            past = self.sc.at(t - 3 * HOUR, la, lo)
            lines.append(f"{sid}  {la:8.3f} {lo:8.3f} {g.tm_year} {g.tm_mon:02d} {g.tm_mday:02d} {g.tm_hour:02d} "
                         f"{g.tm_min:02d} {r['dir']:3.0f} {r['wind'] * 0.514444 * 1.1:5.1f} {r['gust'] * 0.514444:5.1f} "
                         f"{r['wave']:4.1f} {r['period'] + 1:4.0f}  MM {r['wave_dir']:3.0f} {r['pressure']:6.1f} "
                         f"{r['pressure'] - past['pressure']:+5.1f} {r['temp']:5.1f} {r['sst']:5.1f} {r['dew']:5.1f}   MM     MM")
        return httpx.Response(200, text="\n".join(lines) + "\n")

    # ---- CO-OPS ----
    def _coops(self, path: str, q: dict) -> httpx.Response:
        lake = in_great_lakes(self.home.lat, self.home.lon)
        if path.endswith("stations.json"):
            if lake:
                return httpx.Response(200, json={"count": 0, "stations": []})
            if q.get("type") == "currentpredictions":
                st = {"id": "DMO0001", "name": "Demo Channel (DEMO)", "lat": self.home.lat - 0.04, "lng": self.home.lon + 0.03}
            else:
                st = {"id": "9990001", "name": "Demo Harbor (DEMO)", "lat": self.home.lat + 0.05,
                      "lng": self.home.lon + 0.05, "state": "XX"}
            return httpx.Response(200, json={"count": 1, "stations": [st]})
        begin = calendar.timegm(time.strptime(q["begin_date"], "%Y%m%d %H:%M"))
        end = begin + int(q.get("range", 24)) * HOUR
        period = 12.42 * HOUR
        t_hw = (self.clock() // period) * period + 2 * HOUR

        def fmt(t):
            return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))

        if q.get("product") == "predictions" and q.get("interval") == "hilo":
            preds, k = [], math.floor((begin - t_hw) / (period / 2))
            while (t := t_hw + k * period / 2) < end:
                if t >= begin:
                    high = k % 2 == 0
                    preds.append({"t": fmt(t), "v": f"{0.9 + (0.8 if high else -0.8):.3f}", "type": "H" if high else "L"})
                k += 1
            return httpx.Response(200, json={"predictions": preds})
        if q.get("product") == "predictions":
            preds = [{"t": fmt(t), "v": f"{0.9 + 0.8 * math.cos(2 * math.pi * (t - t_hw) / period):.3f}"}
                     for t in range(int(begin), int(end), HOUR)]
            return httpx.Response(200, json={"predictions": preds})
        if q.get("product") == "currents_predictions":
            cp, k = [], math.floor((begin - t_hw) / (period / 4))
            while (t := t_hw + k * period / 4) < end:
                if t >= begin:
                    phase = k % 4  # 0 slack(HW) 1 ebb max 2 slack(LW) 3 flood max
                    v = {0: 0.0, 1: -2.1, 2: 0.0, 3: 1.8}[phase]
                    cp.append({"Type": "slack" if v == 0 else "ebb" if v < 0 else "flood", "meanFloodDir": 45,
                               "Bin": "1", "meanEbbDir": 225, "Time": fmt(t), "Depth": None, "Velocity_Major": v})
                k += 1
            return httpx.Response(200, json={"current_predictions": {"units": "feet, knots", "cp": cp}})
        return httpx.Response(400, json={"error": {"message": "demo: unsupported"}})


def build_demo_service(settings: Settings) -> WeatherService:
    if settings.home is None:
        settings.home = DEMO_HOME
    now = time.time()
    scenario = Scenario(now, settings.home.lat, settings.home.lon)
    svc = WeatherService(settings, transport=httpx.MockTransport(DemoHandler(scenario, settings.home)))
    svc.demo_scenario = scenario  # type: ignore[attr-defined]
    return svc


def backfill_observations(svc: WeatherService, scenario: Scenario, hours: float = 8) -> None:
    """Give the demo a barograph and wind history so tendency and nowcast have data."""
    now = svc.clock()
    if svc.db.observations("pressure_hpa", now - 3600):
        return
    rng = random.Random(42)
    home = svc.settings.home
    rows = []
    b = INSTRUMENT_BIAS
    for i in range(int(hours * 60), 0, -1):
        t = now - i * 60
        r = scenario.at(t, home.lat, home.lon)
        tws = max(0.5, r["wind"] * b["ratio"] * (1 + rng.gauss(0, 0.06)))
        rows += [(t, "tws_kn", round(tws, 2), "demo"),
                 (t, "tws_max_kn", round(tws * (1.2 + abs(rng.gauss(0, 0.08))), 2), "demo"),
                 (t, "twd_deg", round((r["dir"] + b["dir_offset"] + rng.gauss(0, 4)) % 360, 1), "demo"),
                 (t, "pressure_hpa", round(r["pressure"] + b["pressure_offset"] + rng.gauss(0, 0.05), 2), "demo"),
                 (t, "air_temp_c", round(r["temp"], 2), "demo"),
                 (t, "water_temp_c", round(r["sst"], 2), "demo")]
    svc.db.add_observations(rows)
    svc.db.kv_set("demo_backfill", {"at": now, "rows": len(rows)})


async def run_demo_instruments(svc: WeatherService) -> None:
    scenario: Scenario = svc.demo_scenario  # type: ignore[attr-defined]
    backfill_observations(svc, scenario)
    home = svc.settings.home
    sim = BoatSim(home.lat, home.lon, scenario=scenario, bias=INSTRUMENT_BIAS)
    last = time.time()
    while True:
        await asyncio.sleep(1.0)
        now = time.time()
        for line in sim.step(now, now - last):
            svc.hub.feed_nmea(line, "demo-nmea")
        last = now


