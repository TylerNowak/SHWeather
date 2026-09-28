import math

import pytest

from shweather.analysis import beaufort, conditions, nowcast, pressure, zambretti
from shweather.analysis.wind import from_uv, to_uv, true_wind
from shweather.config import BoatProfile
from shweather.units import pressure_to_sea_level

# ---------------------------------------------------------------- Beaufort


@pytest.mark.parametrize("kn,force", [(0, 0), (0.4, 0), (1, 1), (3.4, 1), (6.6, 3), (16.4, 4), (16.6, 5),
                                      (33, 7), (34, 8), (63.4, 11), (64, 12), (120, 12)])
def test_beaufort(kn, force):
    assert beaufort.force(kn) == force


def test_beaufort_describe():
    assert beaufort.describe(35)["description"] == "Gale"
    assert beaufort.describe(None) is None

# ---------------------------------------------------------------- true wind


def test_true_wind_head_to_wind():
    tw = true_wind(aws_kn=15, awa_deg=0, boat_speed_kn=5, heading_deg=0)
    assert tw["tws_kn"] == pytest.approx(10)
    assert tw["twd_deg"] == pytest.approx(0, abs=1e-6)


def test_true_wind_beam_reach():
    # Boat heading north at 6 kn, apparent wind 10 kn at 90 deg (starboard beam).
    tw = true_wind(10, 90, 6, 0)
    # True from-vector = (A cos - V, A sin) = (-6, 10) -> TWS = sqrt(136), TWA ~ 121 deg
    assert tw["tws_kn"] == pytest.approx(math.sqrt(136))
    assert tw["twa_deg"] == pytest.approx(math.degrees(math.atan2(10, -6)))
    assert tw["twd_deg"] == pytest.approx(tw["twa_deg"])


def test_true_wind_at_rest_equals_apparent():
    tw = true_wind(12, 45, 0, 200)
    assert tw["tws_kn"] == pytest.approx(12)
    assert tw["twd_deg"] == pytest.approx(245)


def test_uv_roundtrip():
    s, d = from_uv(*to_uv(14, 300))
    assert s == pytest.approx(14)
    assert d == pytest.approx(300)

# ---------------------------------------------------------------- pressure


def samples(start, rate_per_h, hours=4, step_s=60):
    return [(start + i * step_s, 1015 + rate_per_h * i * step_s / 3600) for i in range(int(hours * 3600 / step_s) + 1)]


def test_tendency_exact_three_hours():
    s = samples(0, -1.2)  # -3.6 over 3 h
    t = pressure.tendency(s, now=s[-1][0])
    assert t["change_hpa"] == pytest.approx(-3.6, abs=0.05)
    assert not t["estimated"]
    assert t["rate"] == "quickly" and t["trend"] == "falling"
    assert any(w["level"] == "warning" for w in t["warnings"])


def test_tendency_estimated_from_short_history():
    s = samples(0, 1.0, hours=1)
    t = pressure.tendency(s, now=s[-1][0])
    assert t["estimated"]
    assert t["change_hpa"] == pytest.approx(3.0, abs=0.1)


def test_tendency_needs_data():
    assert pressure.tendency([(0, 1010)]) is None
    assert pressure.tendency(samples(0, 1, hours=0.5), now=1800) is None


@pytest.mark.parametrize("ch,text", [(0.05, "Steady"), (-1.0, "Falling slowly"), (2.0, "Rising"),
                                     (-4.0, "Falling quickly"), (7.0, "Rising very rapidly")])
def test_classify(ch, text):
    assert pressure.classify(ch)["text"] == text


def test_sea_level_reduction_lake_superior():
    # Lake Superior surface ~183 m: station 995 hPa is roughly 1017 hPa MSL.
    assert pressure_to_sea_level(995, 183, 15) == pytest.approx(1016.8, abs=0.5)
    assert pressure_to_sea_level(1000, 0) == 1000

# ---------------------------------------------------------------- Zambretti


def test_zambretti_extremes():
    assert zambretti.forecast(1040, "rising", 6, 50)["text"] == "Settled fine"
    assert zambretti.forecast(960, "falling", 1, 50)["text"] == "Stormy, much rain"


def test_zambretti_trend_threshold():
    assert zambretti.trend_from_change(1.6) == "rising"
    assert zambretti.trend_from_change(-1.59) == "steady"


def test_zambretti_southern_hemisphere_mirrors_wind():
    north = zambretti.forecast(1012, "steady", 1, 45, wind_dir_deg=0)     # northerly, N hemisphere
    south = zambretti.forecast(1012, "steady", 7, -45, wind_dir_deg=180)  # southerly, S hemisphere, same season
    assert north["letter"] == south["letter"]

# ---------------------------------------------------------------- conditions


def test_assess_levels():
    boat = BoatProfile(reef1_kn=15, reef2_kn=20, max_wind_kn=25, max_gust_kn=32, max_wave_m=2)
    assert conditions.assess({"wind_speed_kn": 10, "wind_gust_kn": 13}, boat)["level"] == "good"
    assert conditions.assess({"wind_speed_kn": 16}, boat)["level"] == "reef"
    assert conditions.assess({"wind_speed_kn": 21}, boat)["level"] == "caution"
    assert conditions.assess({"wind_speed_kn": 26}, boat)["level"] == "nogo"
    assert conditions.assess({"wind_speed_kn": 12, "wind_gust_kn": 33}, boat)["level"] == "nogo"
    assert conditions.assess({"wind_speed_kn": 12, "wave_height_m": 2.1}, boat)["level"] == "nogo"
    assert conditions.assess({"wind_speed_kn": 8, "weather_code": 95}, boat)["level"] == "nogo"
    assert conditions.assess({"wind_speed_kn": 8, "visibility_m": 500}, boat)["level"] == "caution"
    assert conditions.assess({}, boat)["level"] == "unknown"


def test_reason_numbers_never_look_equal_to_the_limit():
    boat = BoatProfile(max_wind_kn=25)
    assert conditions.assess({"wind_speed_kn": 25.3}, boat)["reasons"][0] == "Wind 25.3 kn is above your 25 kn limit"
    assert conditions.assess({"wind_speed_kn": 24.6}, boat)["reasons"][0] == "Wind 24.6 kn: second reef"
    assert conditions.assess({"wind_speed_kn": 27.2}, boat)["reasons"][0] == "Wind 27 kn is above your 25 kn limit"


def test_steep_seas_warning():
    boat = BoatProfile(min_wave_period_ratio=4.0)
    r = conditions.assess({"wind_speed_kn": 12, "wave_height_m": 1.2, "wave_period_s": 4}, boat)
    assert r["level"] == "caution" and "steep" in r["reasons"][0]

# ---------------------------------------------------------------- nowcast


def _fc(now, n=24, wind=10.0, direction=200.0, p=1012.0):
    t0 = int(now // 3600 * 3600) - 8 * 3600
    times = [t0 + i * 3600 for i in range(n)]
    return {"time": times, "wind_speed_kn": [wind] * n, "wind_gust_kn": [wind * 1.3] * n,
            "wind_dir_deg": [direction] * n, "pressure_hpa": [p] * n}


def test_nowcast_learns_and_decays():
    now = 1_800_000_000.0
    fc = _fc(now)
    obs = {"tws_kn": [(now - i * 60, 13.0) for i in range(7 * 60)],
           "twd_deg": [(now - i * 60, 215.0) for i in range(7 * 60)],
           "pressure_hpa": [(now - i * 60, 1013.5) for i in range(7 * 60)]}
    local, bias = nowcast.localize(fc, obs, now)
    assert bias["active"]
    assert bias["wind_ratio"] == pytest.approx(1.3, abs=0.01)
    assert bias["dir_offset_deg"] == pytest.approx(15, abs=0.5)
    assert bias["pressure_offset_hpa"] == pytest.approx(1.5, abs=0.01)
    i_now = next(i for i, t in enumerate(fc["time"]) if t >= now)
    near, far = local["wind_speed_kn"][i_now], local["wind_speed_kn"][-1]
    assert 12 < near <= 13.0            # almost the full correction right now
    assert 10.0 <= far < 10.5          # back to the model later
    assert local["wind_speed_kn"][0] == 10.0  # past hours untouched
    assert fc["wind_speed_kn"][i_now] == 10.0  # input not mutated


def test_nowcast_inactive_without_observations():
    now = 1_800_000_000.0
    fc = _fc(now)
    local, bias = nowcast.localize(fc, {}, now)
    assert not bias["active"] and local is fc


def test_nowcast_ratio_is_clamped():
    now = 1_800_000_000.0
    fc = _fc(now, wind=5)
    obs = {"tws_kn": [(now - i * 60, 30.0) for i in range(7 * 60)]}
    _, bias = nowcast.localize(fc, obs, now)
    assert bias["wind_ratio"] == nowcast.RATIO_LIMITS[1]
