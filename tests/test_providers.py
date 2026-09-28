import json
from pathlib import Path

import httpx
import pytest

from conftest import fixture_json, fixture_text
from shweather.config import Settings
from shweather.db import Database
from shweather.net import Http
from shweather.providers import NotApplicable, ProviderError, coops, ndbc, nws
from shweather.providers.open_meteo import OpenMeteo, normalize_location

# ---------------------------------------------------------------- NDBC


def test_ndbc_latest_obs_parse_and_units():
    rows = [ndbc.normalize(r) for r in ndbc.parse_table(fixture_text("ndbc_latest_obs.txt"))]
    assert len(rows) == 4
    first = rows[0]
    assert first["station"] == "14049"
    assert first["lat"] == -12.0 and first["lon"] == 65.0
    assert first["wind_speed_kn"] == pytest.approx(9.4 / 0.514444, abs=0.1)
    assert first["wave_height_m"] is None  # MM
    assert first["pressure_hpa"] == 1013.7
    lm = rows[3]
    assert lm["pressure_tendency_hpa"] == -1.2 and lm["wave_height_m"] == 0.9


def test_ndbc_realtime2_two_digit_year_header():
    rows = [ndbc.normalize(r) for r in ndbc.parse_table(fixture_text("ndbc_realtime2_44013.txt"))]
    assert len(rows) == 3
    assert rows[0]["time"] == pytest.approx(1790434800)  # 2026-09-26 15:00Z
    assert rows[1]["wave_height_m"] == 4.1 and rows[1]["wave_dir_deg"] == 96
    assert rows[0]["pressure_tendency_hpa"] == 0.0


def test_ndbc_nearest_filters_by_age_and_radius():
    rows = [ndbc.normalize(r) for r in ndbc.parse_table(fixture_text("ndbc_latest_obs.txt"))]
    now = rows[3]["time"] + 600
    near = ndbc.nearest(rows, 42.5, -87.2, radius_nm=60, now=now)
    assert [s["station"] for s in near] == ["45007"]
    assert near[0]["distance_nm"] < 20
    assert ndbc.nearest(rows, 42.5, -87.2, radius_nm=60, now=now + 7 * 3600) == []

# ---------------------------------------------------------------- CO-OPS


def test_coops_currents():
    ev = coops.normalize_currents(fixture_json("coops_currents.json"))
    assert [e["type"] for e in ev] == ["flood", "slack", "ebb"]
    assert ev[2]["speed_kn"] == 2.71 and ev[2]["velocity_kn"] == -2.71
    assert ev[0]["flood_dir_deg"] == 170


def test_coops_hilo():
    ev = coops.normalize_hilo(fixture_json("coops_hilo.json"))
    assert [e["type"] for e in ev] == ["high", "low", "high"]
    assert ev[1]["height_m"] == -0.052


def test_coops_error_payload():
    with pytest.raises(ProviderError):
        coops.normalize_hilo({"error": {"message": "No Predictions data was found."}})


def test_coops_nearest_station():
    stations = coops.normalize_stations({"stations": [
        {"id": "1", "name": "A", "lat": 41.0, "lng": -71.0}, {"id": "2", "name": "B", "lat": 41.3, "lng": -71.0}]})
    assert coops.nearest_station(stations, 41.25, -71.0, 25)["id"] == "2"
    with pytest.raises(NotApplicable):
        coops.nearest_station(stations, 45, -87, 25)

# ---------------------------------------------------------------- NWS


def test_ugc_ranges_and_lists():
    assert nws.parse_ugc("LMZ740>742-744-282145-") == {"LMZ740", "LMZ741", "LMZ742", "LMZ744"}
    assert nws.parse_ugc("ANZ330-GMZ031>032-011530-") == {"ANZ330", "GMZ031", "GMZ032"}


def test_extract_zone_text_multiline_ugc():
    text = fixture_text("nws_nsh_lot.txt")
    first = nws.extract_zone_text(text, "lmz741")
    assert first.startswith("Winthrop Harbor") and "SMALL CRAFT ADVISORY" in first
    second = nws.extract_zone_text(text, "LMZ745")
    assert "Michigan City" in second and "SMALL CRAFT" not in second
    assert nws.extract_zone_text(text, "LMZ080") is None


@pytest.mark.parametrize("d,h", [("PT1H", 1), ("PT3H", 3), ("P1DT2H", 26), ("PT30M", 1), ("P2D", 48)])
def test_iso_duration(d, h):
    assert nws.parse_duration_hours(d) == h


def test_gridpoint_normalization():
    payload = {"properties": {
        "updateTime": "2026-09-28T14:00:00+00:00",
        "windSpeed": {"uom": "wmoUnit:km_h-1", "values": [
            {"validTime": "2026-09-28T15:00:00+00:00/PT2H", "value": 18.52}]},
        "waveHeight": {"uom": "wmoUnit:m", "values": [
            {"validTime": "2026-09-28T15:00:00+00:00/PT3H", "value": 1.2},
            {"validTime": "2026-09-28T18:00:00+00:00/PT1H", "value": None}]},
        "waveDirection": {"uom": "wmoUnit:degree_(angle)", "values": [
            {"validTime": "2026-09-28T15:00:00+00:00/PT1H", "value": 210}]},
    }}
    s = nws.normalize_gridpoint(payload)
    assert len(s["time"]) == 3
    assert s["wind_speed_kn"][:2] == [10.0, 10.0] and s["wind_speed_kn"][2] is None
    assert s["wave_height_m"] == [1.2, 1.2, 1.2]
    assert s["wave_dir_deg"][0] == 210


def test_alert_normalization_and_marine_flag():
    a = nws.normalize_alert({"properties": {"event": "Small Craft Advisory", "severity": "Minor",
                                            "expires": "2026-09-29T00:00:00Z", "areaDesc": "LMZ741"}})
    assert a["marine"] and a["ends"] == "2026-09-29T00:00:00Z"
    assert not nws.normalize_alert({"properties": {"event": "Heat Advisory"}})["marine"]


def test_is_marine_zone():
    assert nws.is_marine_zone("LMZ741") and nws.is_marine_zone("GMZ031")
    assert not nws.is_marine_zone("ILZ014")

# ---------------------------------------------------------------- Open-Meteo


def _om_location(lat, lon, units_speed="kn"):
    return {"latitude": lat, "longitude": lon, "utc_offset_seconds": 0, "timezone": "GMT",
            "hourly_units": {"time": "unixtime", "wind_speed_10m": units_speed, "wind_direction_10m": "°",
                             "ocean_current_velocity": "km/h", "wave_height": "m"},
            "hourly": {"time": [1790596800, 1790600400], "wind_speed_10m": [10.0, None],
                       "wind_direction_10m": [200, 210], "ocean_current_velocity": [1.852, 3.704],
                       "wave_height": [0.5, None]}}


def test_open_meteo_unit_conversion():
    from shweather.providers.open_meteo import FORECAST_VARS, MARINE_VARS
    s = normalize_location(_om_location(1, 2, "km/h"), {**FORECAST_VARS, **MARINE_VARS})
    assert s["wind_speed_kn"] == [pytest.approx(5.4, abs=0.01), None]
    assert s["current_speed_kn"] == [1.0, 2.0]
    assert s["wave_height_m"] == [0.5, None]


def test_open_meteo_error_object():
    with pytest.raises(ProviderError):
        normalize_location({"error": True, "reason": "Latitude must be in range"}, {})


def _http(handler, tmp_path: Path, **settings):
    st = Settings(data_dir=tmp_path, **settings)
    db = Database(tmp_path / "t.db")
    return Http(st, db, httpx.MockTransport(handler)), db


@pytest.mark.anyio
async def test_open_meteo_multi_location_request(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        q = dict(req.url.params)
        seen.update(q)
        lats = q["latitude"].split(",")
        body = [_om_location(float(a), 0) for a in lats]
        return httpx.Response(200, content=json.dumps(body).encode())

    http, _ = _http(handler, tmp_path)
    om = OpenMeteo(http, forecast_url="https://api.open-meteo.com", marine_url="https://marine-api.open-meteo.com")
    res = await om.forecast([(41.0, -87.5), (41.25, -87.5)])
    assert len(res) == 2 and res[0]["wind_speed_kn"][0] == 10.0
    assert seen["latitude"] == "41,41.25" and seen["cell_selection"] == "sea"
    assert seen["wind_speed_unit"] == "kn" and seen["timeformat"] == "unixtime" and seen["past_days"] == "1"
    await http.aclose()


@pytest.mark.anyio
async def test_open_meteo_http_error(tmp_path):
    http, _ = _http(lambda r: httpx.Response(400, json={"error": True, "reason": "bad"}), tmp_path)
    om = OpenMeteo(http, forecast_url="https://x", marine_url="https://y")
    with pytest.raises(ProviderError, match="bad"):
        await om.forecast([(0, 0)])
    await http.aclose()


@pytest.mark.anyio
async def test_nws_point_404_is_not_applicable(tmp_path):
    http, _ = _http(lambda r: httpx.Response(404, json={"detail": "Unable to provide data"}), tmp_path)
    with pytest.raises(NotApplicable):
        await nws.NWS(http).point(10, -30)
    await http.aclose()
