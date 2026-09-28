"""Zambretti forecaster: a barometer-only local forecast that needs no internet.

Negretti & Zambra's 1915 "Zambretti" algorithm, following the widely used Beteljuice
reconstruction. Inputs are MSL pressure, the 3-hour trend, the wind direction and the
season. It was tuned for the mid-latitude westerlies (UK); treat it as a heuristic, most
useful mid-passage when nothing else is available. Accuracy claims of ~90% are folklore.
"""

from __future__ import annotations

FORECASTS = [
    "Settled fine", "Fine weather", "Becoming fine", "Fine, becoming less settled",
    "Fine, possible showers", "Fairly fine, improving", "Fairly fine, possible showers early",
    "Fairly fine, showery later", "Showery early, improving", "Changeable, mending",
    "Fairly fine, showers likely", "Rather unsettled, clearing later", "Unsettled, probably improving",
    "Showery, bright intervals", "Showery, becoming less settled", "Changeable, some rain",
    "Unsettled, short fine intervals", "Unsettled, rain later", "Unsettled, some rain",
    "Mostly very unsettled", "Occasional rain, worsening", "Rain at times, very unsettled",
    "Rain at frequent intervals", "Rain, very unsettled", "Stormy, may improve", "Stormy, much rain",
]

# Index into FORECASTS for each of 22 pressure bands (low -> high).
RISE = [25, 25, 25, 24, 24, 19, 16, 12, 11, 9, 8, 6, 5, 2, 1, 1, 0, 0, 0, 0, 0, 0]
STEADY = [25, 25, 25, 25, 25, 25, 23, 23, 22, 18, 15, 13, 10, 4, 1, 1, 0, 0, 0, 0, 0, 0]
FALL = [25, 25, 25, 25, 25, 25, 25, 25, 23, 23, 21, 20, 17, 14, 7, 3, 1, 1, 1, 0, 0, 0]

BARO_TOP = 1050.0
BARO_BOTTOM = 950.0
RANGE = BARO_TOP - BARO_BOTTOM

# Pressure adjustment (% of range) by 16-point wind direction, northern hemisphere.
WIND_ADJUST_N = [6, 5, 5, 2, -0.5, -2, -5, -8.5, -12, -10, -6, -4.5, -3, -0.5, 1.5, 3]

TREND_THRESHOLD_HPA = 1.6  # 3-hour change considered "rising"/"falling"


def trend_from_change(change_hpa: float) -> str:
    if change_hpa >= TREND_THRESHOLD_HPA:
        return "rising"
    if change_hpa <= -TREND_THRESHOLD_HPA:
        return "falling"
    return "steady"


def forecast(pressure_hpa: float, trend: str, month: int, lat: float,
             wind_dir_deg: float | None = None) -> dict:
    north = lat >= 0
    p = pressure_hpa
    if wind_dir_deg is not None:
        idx = int(((wind_dir_deg % 360) / 22.5) + 0.5) % 16
        if not north:
            idx = (idx + 8) % 16  # mirror: a southerly in the south behaves like a northerly in the north
        p += WIND_ADJUST_N[idx] / 100.0 * RANGE
    summer = 4 <= month <= 9
    if not north:
        summer = not summer
    if summer:
        if trend == "rising":
            p += 7 / 100.0 * RANGE
        elif trend == "falling":
            p -= 7 / 100.0 * RANGE
    if p >= BARO_TOP:
        p = BARO_TOP - 1
    band = int((p - BARO_BOTTOM) / (RANGE / 22))
    band = max(0, min(21, band))
    table = RISE if trend == "rising" else FALL if trend == "falling" else STEADY
    i = table[band]
    return {"letter": chr(ord("A") + i), "text": FORECASTS[i], "trend": trend,
            "method": "zambretti", "note": "Barometric heuristic; works offline, low precision."}
