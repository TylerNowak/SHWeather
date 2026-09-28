"""Barometric tendency: the sailor's oldest forecasting tool.

Tendency terms follow the definitions used in UK/WMO marine broadcasts for the change over
the previous three hours:

    steady          < 0.1 hPa
    slowly          0.1 - 1.5 hPa
    (rising|falling) 1.6 - 3.5 hPa
    quickly         3.6 - 6.0 hPa
    very rapidly    > 6.0 hPa
"""

from __future__ import annotations

from collections.abc import Sequence

THREE_HOURS = 3 * 3600


def change_3h(samples: Sequence[tuple[float, float]], now: float | None = None) -> dict | None:
    """Pressure change over the last 3 h from (ts, hPa) samples sorted by time.

    Uses the reading nearest to now-3h if one exists within 20 minutes; otherwise, with at
    least 45 minutes of data, extrapolates a least-squares slope to 3 h (flagged estimated).
    """
    if len(samples) < 2:
        return None
    now = now or samples[-1][0]
    latest_ts, latest = samples[-1]
    if now - latest_ts > 1800:
        return None  # barometer has gone quiet
    target = latest_ts - THREE_HOURS
    past = min(samples, key=lambda s: abs(s[0] - target))
    if abs(past[0] - target) <= 1200:
        return {"change_hpa": round(latest - past[1], 1), "estimated": False, "span_s": latest_ts - past[0]}
    window = [s for s in samples if s[0] >= target]
    span = window[-1][0] - window[0][0] if window else 0
    if len(window) < 3 or span < 2700:
        return None
    n = len(window)
    mx = sum(s[0] for s in window) / n
    my = sum(s[1] for s in window) / n
    sxx = sum((s[0] - mx) ** 2 for s in window)
    if sxx == 0:
        return None
    slope = sum((s[0] - mx) * (s[1] - my) for s in window) / sxx
    return {"change_hpa": round(slope * THREE_HOURS, 1), "estimated": True, "span_s": span}


def classify(change_hpa: float) -> dict:
    a = abs(change_hpa)
    direction = "rising" if change_hpa > 0 else "falling"
    if a < 0.1:
        return {"trend": "steady", "rate": "steady", "text": "Steady"}
    if a <= 1.5:
        rate = "slowly"
    elif a <= 3.5:
        rate = "normal"
    elif a <= 6.0:
        rate = "quickly"
    else:
        rate = "very_rapidly"
    text = {"slowly": f"{direction.capitalize()} slowly", "normal": direction.capitalize(),
            "quickly": f"{direction.capitalize()} quickly", "very_rapidly": f"{direction.capitalize()} very rapidly"}[rate]
    return {"trend": direction, "rate": rate, "text": text}


def warnings(change_hpa: float, pressure_hpa: float | None = None) -> list[dict]:
    """Rules of thumb from seamanship manuals. Deliberately conservative."""
    out = []

    def add(level: str, code: str, text: str, **values) -> None:
        out.append({"level": level, "code": code, "text": text, "change_hpa": change_hpa, **values})

    if change_hpa < -6.0:
        add("danger", "falling_very_rapidly", "Barometer falling very rapidly: storm-force winds possible. Seek shelter.")
    elif change_hpa <= -3.6:
        add("warning", "falling_quickly", "Barometer falling quickly: gale possible within hours.")
    elif change_hpa <= -1.6:
        add("caution", "falling", "Barometer falling: expect wind to increase and weather to deteriorate.")
    if change_hpa > 6.0:
        add("warning", "rising_very_rapidly", "Barometer rising very rapidly: strong, squally winds likely behind the low.")
    elif change_hpa >= 3.6:
        add("caution", "rising_quickly", "Barometer rising quickly: a blustery, gusty spell is likely.")
    if pressure_hpa is not None and pressure_hpa < 990 and change_hpa < 0:
        add("warning", "deep_low", f"Deep low ({pressure_hpa:.0f} hPa) and still falling.", pressure_hpa=pressure_hpa)
    return out


def tendency(samples: Sequence[tuple[float, float]], now: float | None = None) -> dict | None:
    ch = change_3h(samples, now)
    if ch is None:
        return None
    return {**ch, **classify(ch["change_hpa"]), "warnings": warnings(ch["change_hpa"], samples[-1][1])}
