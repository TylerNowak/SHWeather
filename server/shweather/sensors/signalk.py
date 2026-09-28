"""Signal K input (e.g. OpenPlotter, or any boat already running a Signal K server).

Polls the REST API for ``vessels/self`` every few seconds; no websocket dependency needed.
Signal K is SI: m/s, radians, Kelvin, Pascal, ratios. Values older than two minutes are
ignored. If the server's derived-data plugin provides true wind, we use it directly.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math

import httpx

from ..config import SignalKConfig
from ..units import kelvin_to_c, ms_to_kn
from .hub import SensorHub

log = logging.getLogger(__name__)
MAX_AGE_S = 120


def _node(tree: dict, path: str):
    cur = tree
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _val(tree: dict, path: str, now: float):
    n = _node(tree, path)
    if not isinstance(n, dict) or n.get("value") is None:
        return None
    ts = n.get("timestamp")
    if ts:
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if now - t > MAX_AGE_S:
                return None
        except ValueError:
            pass
    return n["value"]


def _deg(rad):
    return None if rad is None else math.degrees(rad) % 360


def extract(tree: dict, now: float) -> dict:
    """Map a Signal K vessels/self tree to canonical metrics."""
    v = lambda p: _val(tree, p, now)  # noqa: E731
    out: dict = {}
    pos = v("navigation.position")
    if isinstance(pos, dict) and pos.get("latitude") is not None:
        out["lat"], out["lon"] = pos["latitude"], pos.get("longitude")
    out["sog_kn"] = ms_to_kn(v("navigation.speedOverGround"))
    out["cog_deg"] = _deg(v("navigation.courseOverGroundTrue"))
    out["stw_kn"] = ms_to_kn(v("navigation.speedThroughWater"))
    out["heading_true_deg"] = _deg(v("navigation.headingTrue"))
    out["heading_mag_deg"] = _deg(v("navigation.headingMagnetic"))
    var = v("navigation.magneticVariation")
    out["mag_variation_deg"] = None if var is None else math.degrees(var)
    out["aws_kn"] = ms_to_kn(v("environment.wind.speedApparent"))
    out["awa_deg"] = _deg(v("environment.wind.angleApparent"))
    twd = _deg(v("environment.wind.directionTrue"))
    tws = v("environment.wind.speedOverGround")
    if tws is None:
        tws = v("environment.wind.speedTrue")
    if twd is not None and tws is not None:
        out["twd_deg"], out["tws_kn"] = twd, ms_to_kn(tws)
    p = v("environment.outside.pressure")
    out["pressure_hpa"] = None if p is None else p / 100.0
    out["air_temp_c"] = kelvin_to_c(v("environment.outside.temperature"))
    rh = v("environment.outside.relativeHumidity")
    if rh is None:
        rh = v("environment.outside.humidity")
    out["humidity_pct"] = None if rh is None else (rh * 100 if rh <= 1.0 else rh)
    out["dewpoint_c"] = kelvin_to_c(v("environment.outside.dewPointTemperature"))
    out["water_temp_c"] = kelvin_to_c(v("environment.water.temperature"))
    return {k: val for k, val in out.items() if val is not None}


async def run_signalk(cfg: SignalKConfig, hub: SensorHub) -> None:
    name = f"signalk:{cfg.url}"
    url = cfg.url.rstrip("/") + "/signalk/v1/api/vessels/self"
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        backoff = 2.0
        while True:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                values = extract(resp.json(), hub.clock())
                st = hub.sources.setdefault(name, {"sentences": 0, "useful": 0})
                st.update(connected=True, last_seen=hub.clock())
                st["sentences"] += 1
                if values:
                    st["useful"] += 1
                    hub.update(values, name)
                backoff = 2.0
                await asyncio.sleep(cfg.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                hub.mark_source(name, connected=False, error=str(exc))
                log.warning("Signal K %s: %s", cfg.url, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
