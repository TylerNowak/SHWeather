"""True wind from apparent wind and boat motion."""

from __future__ import annotations

import math


def true_wind(aws_kn: float, awa_deg: float, boat_speed_kn: float, heading_deg: float,
              course_deg: float | None = None) -> dict:
    """Compute true wind speed/direction.

    awa_deg: apparent wind angle relative to the bow, 0-360 clockwise (starboard positive).
    boat_speed_kn / course_deg: use SOG + COG for wind over ground (comparable with
    forecasts), or STW + heading for wind over water. If course is None the boat is
    assumed to move along its heading.

    Returns tws_kn, twd_deg (direction the wind blows FROM, true) and twa_deg (relative).
    """
    course = heading_deg if course_deg is None else course_deg
    # Apparent wind as a velocity vector (blowing towards) in east/north components.
    a_from = math.radians(heading_deg + awa_deg)
    ax, ay = -aws_kn * math.sin(a_from), -aws_kn * math.cos(a_from)
    # Boat velocity over ground/water.
    c = math.radians(course)
    bx, by = boat_speed_kn * math.sin(c), boat_speed_kn * math.cos(c)
    # True wind velocity = apparent + boat velocity.
    tx, ty = ax + bx, ay + by
    tws = math.hypot(tx, ty)
    twd = (math.degrees(math.atan2(-tx, -ty)) + 360.0) % 360.0 if tws > 1e-6 else heading_deg
    twa = (twd - heading_deg) % 360.0
    return {"tws_kn": tws, "twd_deg": twd, "twa_deg": twa}


def to_uv(speed: float, direction_from_deg: float) -> tuple[float, float]:
    r = math.radians(direction_from_deg)
    return -speed * math.sin(r), -speed * math.cos(r)


def from_uv(u: float, v: float) -> tuple[float, float]:
    speed = math.hypot(u, v)
    return speed, (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
