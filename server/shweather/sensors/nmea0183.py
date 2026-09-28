"""Minimal NMEA 0183 parser for the sentences a weather station cares about.

Returns a dict of canonical metrics per sentence (empty if the sentence is irrelevant,
invalid or fails its checksum). Talker IDs are ignored (WIMWV, IIMWV, ...).

Metrics produced
    lat, lon                 position (RMC, GGA, GLL)
    sog_kn, cog_deg          speed/course over ground (RMC, VTG)
    stw_kn                   speed through water (VHW)
    heading_true_deg         (HDT, HDG with variation, VHW)
    heading_mag_deg          (HDM, HDG)
    mag_variation_deg        east positive (RMC, HDG)
    aws_kn, awa_deg          apparent wind (MWV R, VWR)
    tws_kn, twa_deg          true wind relative to bow (MWV T)
    twd_deg                  true wind direction (MWD, MDA)
    pressure_hpa             barometer (MDA, XDR), station pressure
    air_temp_c, water_temp_c, humidity_pct, dewpoint_c (MDA, MTW, XDR)
"""

from __future__ import annotations

from functools import reduce

SPEED_TO_KN = {"N": 1.0, "M": 1.0 / 0.514444, "K": 1.0 / 1.852, "S": 0.868976}


def checksum_ok(sentence: str) -> bool:
    if "*" not in sentence:
        return True  # some instruments omit the checksum
    body, _, cs = sentence[1:].partition("*")
    try:
        return reduce(lambda a, c: a ^ ord(c), body, 0) == int(cs[:2], 16)
    except ValueError:
        return False


def with_checksum(body: str) -> str:
    """Build a full sentence from its body (without leading $): used by the simulator."""
    cs = reduce(lambda a, c: a ^ ord(c), body, 0)
    return f"${body}*{cs:02X}"


def _f(fields: list[str], i: int) -> float | None:
    try:
        v = fields[i]
    except IndexError:
        return None
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _latlon(fields: list[str], i: int) -> tuple[float, float] | None:
    """Parse ddmm.mmmm,N,dddmm.mmmm,W starting at index i."""
    try:
        la, ns, lo, ew = fields[i:i + 4]
        if not la or not lo:
            return None
        lat = int(float(la) / 100) + (float(la) % 100) / 60.0
        lon = int(float(lo) / 100) + (float(lo) % 100) / 60.0
    except (ValueError, IndexError):
        return None
    if ns == "S":
        lat = -lat
    if ew == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _speed(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    return value * SPEED_TO_KN.get(unit.upper(), 1.0)


# Minimum field counts (after the address). Enforced for sentences without a checksum,
# where truncation cannot otherwise be detected.
MIN_FIELDS = {"RMC": 9, "GGA": 6, "GLL": 6, "VTG": 6, "MWV": 5, "VWR": 4, "MWD": 6, "MDA": 18,
              "MTW": 2, "XDR": 4, "HDT": 2, "HDM": 2, "HDG": 5, "VHW": 6}


def parse(line: str) -> dict:
    line = line.strip()
    if not line.startswith("$") or len(line) < 7:
        return {}
    if not checksum_ok(line):
        return {}
    has_checksum = "*" in line
    body = line[1:].split("*", 1)[0]
    fields = body.split(",")
    addr = fields[0]
    if addr.startswith("P"):
        return {}  # proprietary
    kind = addr[-3:]
    f = fields[1:]
    handler = _HANDLERS.get(kind)
    if not handler:
        return {}
    if not has_checksum and len(f) < MIN_FIELDS.get(kind, 0):
        return {}
    try:
        return {k: v for k, v in handler(f).items() if v is not None}
    except (IndexError, ValueError):
        return {}


def _rmc(f: list[str]) -> dict:
    # time, status, lat, N/S, lon, E/W, sog, cog, date, magvar, E/W, [mode]
    if len(f) > 1 and f[1] != "A":
        return {}
    out: dict = {}
    pos = _latlon(f, 2)
    if pos:
        out["lat"], out["lon"] = pos
    out["sog_kn"] = _f(f, 6)
    out["cog_deg"] = _f(f, 7)
    var = _f(f, 9)
    if var is not None:
        out["mag_variation_deg"] = -var if len(f) > 10 and f[10] == "W" else var
    return out


def _gga(f: list[str]) -> dict:
    # time, lat, N/S, lon, E/W, quality, sats, hdop, alt, M, ...
    if len(f) > 5 and f[5] in ("", "0"):
        return {}
    pos = _latlon(f, 1)
    return {"lat": pos[0], "lon": pos[1]} if pos else {}


def _gll(f: list[str]) -> dict:
    # lat, N/S, lon, E/W, time, status
    if len(f) > 5 and f[5] != "A":
        return {}
    pos = _latlon(f, 0)
    return {"lat": pos[0], "lon": pos[1]} if pos else {}


def _vtg(f: list[str]) -> dict:
    # cog true, T, cog mag, M, sog kn, N, sog km/h, K
    sog = _f(f, 4)
    if sog is None and _f(f, 6) is not None:
        sog = _f(f, 6) / 1.852
    return {"cog_deg": _f(f, 0), "sog_kn": sog}


def _mwv(f: list[str]) -> dict:
    # angle, R/T, speed, unit, status
    if len(f) > 4 and f[4] != "A":
        return {}
    angle, ref, speed = _f(f, 0), f[1], _speed(_f(f, 2), f[3] if len(f) > 3 else "N")
    if angle is None or speed is None:
        return {}
    if ref == "R":
        return {"awa_deg": angle % 360, "aws_kn": speed}
    if ref == "T":
        return {"twa_deg": angle % 360, "tws_kn": speed}
    return {}


def _vwr(f: list[str]) -> dict:
    # angle, L/R, kn, N, m/s, M, km/h, K
    angle = _f(f, 0)
    if angle is None:
        return {}
    awa = (360 - angle) % 360 if f[1] == "L" else angle
    speed = _f(f, 2)
    if speed is None and _f(f, 4) is not None:
        speed = _f(f, 4) / 0.514444
    return {"awa_deg": awa, "aws_kn": speed}


def _mwd(f: list[str]) -> dict:
    # dir true, T, dir mag, M, speed kn, N, speed m/s, M
    speed = _f(f, 4)
    if speed is None and _f(f, 6) is not None:
        speed = _f(f, 6) / 0.514444
    return {"twd_deg": _f(f, 0), "tws_kn": speed}


def _mda(f: list[str]) -> dict:
    # 0 baro inHg,I, 2 baro bar,B, 4 air C,C, 6 water C,C, 8 RH, 9 abs hum, 10 dew C,C,
    # 12 wind dir T,T, 14 wind dir M,M, 16 wind kn,N, 18 wind m/s,M
    out: dict = {}
    bar = _f(f, 2)
    inhg = _f(f, 0)
    if bar is not None and bar > 0:
        out["pressure_hpa"] = bar * 1000.0
    elif inhg is not None and inhg > 0:
        out["pressure_hpa"] = inhg / 0.0295300
    out["air_temp_c"] = _f(f, 4)
    out["water_temp_c"] = _f(f, 6)
    out["humidity_pct"] = _f(f, 8)
    out["dewpoint_c"] = _f(f, 10)
    out["twd_deg"] = _f(f, 12)
    ws = _f(f, 16)
    if ws is None and _f(f, 18) is not None:
        ws = _f(f, 18) / 0.514444
    out["tws_kn"] = ws
    return out


def _mtw(f: list[str]) -> dict:
    return {"water_temp_c": _f(f, 0)} if (len(f) < 2 or f[1] in ("C", "")) else {}


_WATER_NAMES = ("WATER", "WTR", "SEA")
_CABIN_NAMES = ("INAIR", "INSIDE", "CABIN")
_AIR_NAMES = ("AIR", "OUTSIDE", "OUTAIR", "ENV_OUT", "TEMPAIR")
_BARO_NAMES = ("BARO", "AIR", "ENV", "ATM", "PRESS")


def _xdr(f: list[str]) -> dict:
    """Transducer groups of 4: type, value, unit, name.

    Only environmental transducers are used. Unnamed transducers are accepted (some weather
    stations send none); anything else (engine, fridge, oil pressure...) is ignored.
    """
    out: dict = {}
    for i in range(0, len(f) - 3, 4):
        typ, val, unit, name = f[i], _f(f, i + 1), f[i + 2].upper(), f[i + 3].upper().split("*")[0]
        if val is None:
            continue
        if typ == "P" and (not name or any(n in name for n in _BARO_NAMES)):
            if unit == "B":
                out["pressure_hpa"] = val * 1000.0
            elif unit == "P":
                out["pressure_hpa"] = val / 100.0
        elif typ == "C" and unit == "C":
            if any(n in name for n in _WATER_NAMES):
                out["water_temp_c"] = val
            elif any(n in name for n in _CABIN_NAMES):
                out["cabin_temp_c"] = val
            elif not name or any(n in name for n in _AIR_NAMES):
                out["air_temp_c"] = val
        elif typ == "H" and unit == "P" and not any(n in name for n in _CABIN_NAMES):
            out["humidity_pct"] = val
    return out


def _hdt(f: list[str]) -> dict:
    return {"heading_true_deg": _f(f, 0)}


def _hdm(f: list[str]) -> dict:
    return {"heading_mag_deg": _f(f, 0)}


def _hdg(f: list[str]) -> dict:
    # heading mag, deviation, E/W, variation, E/W
    hdg = _f(f, 0)
    if hdg is None:
        return {}
    dev = _f(f, 1) or 0.0
    if len(f) > 2 and f[2] == "W":
        dev = -dev
    out = {"heading_mag_deg": (hdg + dev) % 360}
    var = _f(f, 3)
    if var is not None:
        if len(f) > 4 and f[4] == "W":
            var = -var
        out["mag_variation_deg"] = var
        out["heading_true_deg"] = (hdg + dev + var) % 360
    return out


def _vhw(f: list[str]) -> dict:
    # heading T, T, heading M, M, stw kn, N, stw km/h, K
    stw = _f(f, 4)
    if stw is None and _f(f, 6) is not None:
        stw = _f(f, 6) / 1.852
    return {"heading_true_deg": _f(f, 0), "heading_mag_deg": _f(f, 2), "stw_kn": stw}


_HANDLERS = {
    "RMC": _rmc, "GGA": _gga, "GLL": _gll, "VTG": _vtg,
    "MWV": _mwv, "VWR": _vwr, "MWD": _mwd, "MDA": _mda, "MTW": _mtw, "XDR": _xdr,
    "HDT": _hdt, "HDM": _hdm, "HDG": _hdg, "VHW": _vhw,
}
