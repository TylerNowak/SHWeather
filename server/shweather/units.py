"""Unit conversions.

Canonical internal units (see docs/ARCHITECTURE.md): knots, degrees true, hPa (MSL),
metres, seconds, degrees Celsius, Unix seconds UTC.
"""

from __future__ import annotations

KN_PER_MS = 1.0 / 0.514444
KN_PER_KMH = 1.0 / 1.852
KN_PER_MPH = 0.868976
FT_PER_M = 3.28084
M_PER_NM = 1852.0
INHG_PER_HPA = 0.0295300


def ms_to_kn(v: float | None) -> float | None:
    return None if v is None else v * KN_PER_MS


def kmh_to_kn(v: float | None) -> float | None:
    return None if v is None else v * KN_PER_KMH


def kn_to_ms(v: float | None) -> float | None:
    return None if v is None else v / KN_PER_MS


def kelvin_to_c(v: float | None) -> float | None:
    return None if v is None else v - 273.15


def speed_to_kn(value: float | None, unit: str) -> float | None:
    """Convert a speed with a free-form unit label (as APIs report it) to knots."""
    if value is None:
        return None
    u = unit.strip().lower().replace(" ", "")
    if u in ("kn", "kt", "kts", "knots", "knot", "n"):
        return value
    if u in ("m/s", "ms", "m", "wmounit:m_s-1", "ms-1"):
        return value * KN_PER_MS
    if u in ("km/h", "kmh", "k", "wmounit:km_h-1", "kmh-1"):
        return value * KN_PER_KMH
    if u in ("mph", "mp/h", "mi/h"):
        return value * KN_PER_MPH
    raise ValueError(f"unknown speed unit: {unit!r}")


def length_to_m(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    u = unit.strip().lower()
    if u in ("m", "metre", "meter", "wmounit:m"):
        return value
    if u in ("ft", "feet", "wmounit:ft"):
        return value / FT_PER_M
    if u in ("cm",):
        return value / 100.0
    raise ValueError(f"unknown length unit: {unit!r}")


def pressure_to_sea_level(p_hpa: float, altitude_m: float, temp_c: float | None = None) -> float:
    """Reduce station pressure to mean-sea-level pressure (hypsometric approximation).

    Matters on the Great Lakes: Lake Superior sits ~183 m above sea level, which is ~22 hPa
    of difference. Forecast models report MSL pressure, so the onboard barometer must be
    reduced before the two can be compared.
    """
    if altitude_m == 0:
        return p_hpa
    t = 15.0 if temp_c is None else temp_c
    return p_hpa * (1 - (0.0065 * altitude_m) / (t + 0.0065 * altitude_m + 273.15)) ** -5.257
