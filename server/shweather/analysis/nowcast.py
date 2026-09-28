"""Local forecast correction ("nowcast"): bend the model towards what the boat measures.

Grid-scale models miss local effects: sea breezes, funnelling between islands, lake-land
contrasts, a barometer that reads 1.5 hPa high. Over the last few hours we compare the
boat's own 1-minute averaged observations with the forecast for the same hours, then
apply the median speed ratio, the mean direction offset and the pressure offset to the
next hours, fading back to the raw model with a time constant tau.

Cheap, explainable, and it measurably helps the next 3-6 hours, which is the window
that matters for "do I put the reef in now?".
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from ..geo import angle_diff, circular_mean

LOOKBACK_H = 6
MIN_PAIRS = 3
WIND_TAU_H = 3.0
PRESSURE_TAU_H = 12.0
RATIO_LIMITS = (0.5, 2.0)
DIR_LIMIT = 45.0


def _hour_mean(samples: Sequence[tuple[float, float]], t: float, directional: bool = False) -> float | None:
    vals = [v for ts, v in samples if t - 1800 <= ts < t + 1800]
    if len(vals) < 5:  # need at least 5 one-minute averages in the hour
        return None
    return circular_mean(vals) if directional else sum(vals) / len(vals)


def compute_bias(forecast: dict, obs: dict[str, Sequence[tuple[float, float]]], now: float) -> dict:
    times = forecast.get("time") or []
    fc_ws = forecast.get("wind_speed_kn") or []
    fc_wd = forecast.get("wind_dir_deg") or []
    fc_p = forecast.get("pressure_hpa") or []
    ratios, dir_diffs, p_diffs = [], [], []
    for i, t in enumerate(times):
        if not (now - LOOKBACK_H * 3600 <= t <= now):
            continue
        ows = _hour_mean(obs.get("tws_kn", []), t)
        if ows is not None and i < len(fc_ws) and fc_ws[i] is not None and fc_ws[i] >= 4:
            ratios.append(ows / fc_ws[i])
            owd = _hour_mean(obs.get("twd_deg", []), t, directional=True)
            if owd is not None and i < len(fc_wd) and fc_wd[i] is not None and fc_ws[i] >= 6 and ows >= 6:
                dir_diffs.append(angle_diff(owd, fc_wd[i]))
        op = _hour_mean(obs.get("pressure_hpa", []), t)
        if op is not None and i < len(fc_p) and fc_p[i] is not None:
            p_diffs.append(op - fc_p[i])

    bias = {"wind_ratio": None, "dir_offset_deg": None, "pressure_offset_hpa": None,
            "pairs": {"wind": len(ratios), "direction": len(dir_diffs), "pressure": len(p_diffs)},
            "lookback_h": LOOKBACK_H}
    if len(ratios) >= MIN_PAIRS:
        bias["wind_ratio"] = round(min(max(statistics.median(ratios), RATIO_LIMITS[0]), RATIO_LIMITS[1]), 3)
    if len(dir_diffs) >= MIN_PAIRS:
        # Mean of signed differences (they are already small angles around 0).
        off = sum(dir_diffs) / len(dir_diffs)
        bias["dir_offset_deg"] = round(max(-DIR_LIMIT, min(DIR_LIMIT, off)), 1)
    if len(p_diffs) >= 2:
        bias["pressure_offset_hpa"] = round(sum(p_diffs) / len(p_diffs), 2)
    return bias


def apply_bias(forecast: dict, bias: dict, now: float) -> dict:
    """Return a copy of the forecast with corrections applied to future hours."""
    out = {k: list(v) if isinstance(v, list) else v for k, v in forecast.items()}
    times = out.get("time") or []
    r = bias.get("wind_ratio")
    d = bias.get("dir_offset_deg")
    po = bias.get("pressure_offset_hpa")
    for i, t in enumerate(times):
        dt_h = (t - now) / 3600.0
        if dt_h < -0.5:
            continue
        dt_h = max(dt_h, 0.0)
        ww = math.exp(-dt_h / WIND_TAU_H)
        if r is not None:
            f = 1 + (r - 1) * ww
            for key in ("wind_speed_kn", "wind_gust_kn"):
                col = out.get(key)
                if col and col[i] is not None:
                    col[i] = round(col[i] * f, 1)
        if d is not None and out.get("wind_dir_deg") and out["wind_dir_deg"][i] is not None:
            out["wind_dir_deg"][i] = round((out["wind_dir_deg"][i] + d * ww) % 360, 0)
        if po is not None and out.get("pressure_hpa") and out["pressure_hpa"][i] is not None:
            out["pressure_hpa"][i] = round(out["pressure_hpa"][i] + po * math.exp(-dt_h / PRESSURE_TAU_H), 1)
    return out


def localize(forecast: dict, obs: dict[str, Sequence[tuple[float, float]]], now: float) -> tuple[dict, dict]:
    bias = compute_bias(forecast, obs, now)
    active = any(bias[k] is not None for k in ("wind_ratio", "dir_offset_deg", "pressure_offset_hpa"))
    bias["active"] = active
    return (apply_bias(forecast, bias, now) if active else forecast), bias
