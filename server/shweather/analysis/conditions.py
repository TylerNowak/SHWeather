"""Go / reef / caution / no-go assessment against the boat's own limits."""

from __future__ import annotations

from ..config import BoatProfile
from ..units import M_PER_NM

LEVELS = ["good", "reef", "caution", "nogo"]
THUNDER_CODES = {95, 96, 99}


def _worse(a: str, b: str) -> str:
    return a if LEVELS.index(a) >= LEVELS.index(b) else b


def _kn(v: float, *thresholds: float) -> str:
    """Whole knots, unless rounding would make a value look like it crossed (or sat on) a limit."""
    if any((round(v) >= t) != (v >= t) or (round(v) == t and v != t) for t in thresholds):
        return f"{v:.1f}"
    return f"{v:.0f}"


def assess(row: dict, boat: BoatProfile) -> dict:
    """Assess one hour (a dict with canonical fields). Reasons are listed worst first.

    ``reasons`` are ready-made English strings in canonical units (knots, metres, nm).
    ``details`` carry the same reasons as a code plus canonical values, so clients can
    word them in the user's own units (metric / imperial / nautical).
    """
    wind = row.get("wind_speed_kn")
    gust = row.get("wind_gust_kn")
    wave = row.get("wave_height_m")
    period = row.get("wave_period_s")
    vis = row.get("visibility_m")
    code = row.get("weather_code")
    cape = row.get("cape_jkg")

    if wind is None:
        return {"level": "unknown", "reasons": ["No wind data"], "details": [{"level": "unknown", "code": "no_wind"}]}

    found: list[tuple[str, str, dict]] = []

    def add(lvl: str, text: str, code: str, **values) -> None:
        found.append((lvl, text, {"level": lvl, "code": code, **values}))

    wind_limits = [boat.reef1_kn, boat.reef2_kn, boat.max_wind_kn]
    gust_limits = [boat.max_gust_kn, boat.reef1_kn]
    w = _kn(wind, *wind_limits)

    if wind >= boat.max_wind_kn:
        add("nogo", f"Wind {w} kn is above your {boat.max_wind_kn:g} kn limit", "wind_over_limit",
            wind_kn=wind, limit_kn=boat.max_wind_kn, limits_kn=wind_limits)
    elif wind >= boat.reef2_kn:
        add("caution", f"Wind {w} kn: second reef", "second_reef", wind_kn=wind, limit_kn=boat.reef2_kn,
            limits_kn=wind_limits)
    elif wind >= boat.reef1_kn:
        add("reef", f"Wind {w} kn: first reef", "first_reef", wind_kn=wind, limit_kn=boat.reef1_kn,
            limits_kn=wind_limits)
    elif wind < 4:
        add("good", "Light air: expect to motor", "light_air", wind_kn=wind)

    if gust is not None:
        g = _kn(gust, boat.max_gust_kn, boat.reef1_kn)
        if gust >= boat.max_gust_kn:
            add("nogo", f"Gusts {g} kn are above your {boat.max_gust_kn:g} kn limit", "gust_over_limit",
                gust_kn=gust, limit_kn=boat.max_gust_kn, limits_kn=gust_limits)
        elif gust >= 0.85 * boat.max_gust_kn:
            add("caution", f"Gusts {g} kn near your {boat.max_gust_kn:g} kn limit", "gust_near_limit",
                gust_kn=gust, limit_kn=boat.max_gust_kn, limits_kn=gust_limits)
        elif gust >= boat.reef1_kn and wind < boat.reef1_kn:
            add("reef", f"Gusty ({g} kn): consider reefing early", "gusty_reef_early",
                gust_kn=gust, limit_kn=boat.reef1_kn, limits_kn=gust_limits)

    if wave is not None:
        if wave >= boat.max_wave_m:
            add("nogo", f"Waves {wave:.1f} m exceed your {boat.max_wave_m:g} m limit", "waves_over_limit",
                wave_m=wave, limit_m=boat.max_wave_m)
        elif wave >= 0.75 * boat.max_wave_m:
            add("caution", f"Waves {wave:.1f} m near your limit", "waves_near_limit",
                wave_m=wave, limit_m=boat.max_wave_m)
        if (boat.min_wave_period_ratio > 0 and period is not None and wave >= 0.5
                and period < boat.min_wave_period_ratio * wave):
            add("caution", f"Short, steep seas ({wave:.1f} m @ {period:.0f} s)", "steep_seas",
                wave_m=wave, period_s=period)

    if vis is not None and vis < boat.min_visibility_nm * M_PER_NM:
        add("caution", f"Poor visibility ({vis / M_PER_NM:.1f} nm)", "poor_visibility",
            visibility_m=vis, limit_m=boat.min_visibility_nm * M_PER_NM)

    if code is not None and int(code) in THUNDER_CODES:
        add("nogo", "Thunderstorms forecast", "thunderstorms")
    elif cape is not None and cape >= 1000:
        add("caution", "Unstable air: squalls and thunderstorms possible", "unstable_air", cape_jkg=cape)

    level = "good"
    for lvl, _, _ in found:
        level = _worse(level, lvl)
    found.sort(key=lambda r: -LEVELS.index(r[0]))  # stable: keeps wind before gusts within a level
    return {"level": level, "reasons": [text for _, text, _ in found], "details": [d for _, _, d in found]}


def assess_series(series: dict, boat: BoatProfile) -> list[dict]:
    n = len(series.get("time") or [])
    keys = [k for k in series if isinstance(series[k], list)]
    return [assess({k: series[k][i] if i < len(series[k]) else None for k in keys}, boat) for i in range(n)]
