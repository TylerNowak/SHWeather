"""Beaufort wind force scale (WMO), knots."""

from __future__ import annotations

# Lower bound (knots, rounded) of each force 1..12.
_LOWER = [1, 4, 7, 11, 17, 22, 28, 34, 41, 48, 56, 64]

DESCRIPTIONS = [
    "Calm", "Light air", "Light breeze", "Gentle breeze", "Moderate breeze", "Fresh breeze",
    "Strong breeze", "Near gale", "Gale", "Strong gale", "Storm", "Violent storm", "Hurricane force",
]

SEA_STATE = [
    "Sea like a mirror", "Ripples without crests", "Small wavelets", "Large wavelets, scattered whitecaps",
    "Small waves, frequent whitecaps", "Moderate waves, many whitecaps, some spray",
    "Large waves, extensive white foam crests", "Sea heaps up, foam blown in streaks",
    "Moderately high waves, crests break into spindrift", "High waves, dense foam, spray affects visibility",
    "Very high waves with overhanging crests", "Exceptionally high waves", "Air filled with foam and spray",
]


def force(speed_kn: float | None) -> int | None:
    if speed_kn is None:
        return None
    s = round(speed_kn)
    return sum(1 for b in _LOWER if s >= b)


def describe(speed_kn: float | None) -> dict | None:
    f = force(speed_kn)
    if f is None:
        return None
    return {"force": f, "description": DESCRIPTIONS[f], "sea": SEA_STATE[f]}
