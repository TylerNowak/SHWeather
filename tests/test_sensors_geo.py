import pytest

from shweather.config import NmeaSource, Settings
from shweather.geo import bbox_of, distance_nm, grid_for_radius, make_grid
from shweather.sensors.hub import SensorHub
from shweather.sensors.nmea0183 import parse, with_checksum
from shweather.sensors.readers import source_name


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def hub():
    c = Clock()
    return SensorHub(Settings(), clock=c), c


def test_true_wind_derived_from_apparent_when_sailing():
    h, c = hub()
    h.update({"sog_kn": 5.0, "cog_deg": 0.0, "heading_true_deg": 0.0}, "gps")
    h.update({"aws_kn": 15.0, "awa_deg": 0.0}, "wind")
    assert h.value("tws_kn") == pytest.approx(10.0)
    assert h.value("twd_deg") == pytest.approx(0.0, abs=0.01)


def test_no_true_wind_direction_at_anchor_without_compass():
    h, c = hub()
    h.update({"sog_kn": 0.1, "cog_deg": 237.0}, "gps")   # GPS course is noise at rest
    h.update({"aws_kn": 15.0, "awa_deg": 30.0}, "wind")
    assert h.value("twd_deg") is None


def test_instrument_true_wind_is_not_mixed_with_derived():
    h, c = hub()
    h.update({"sog_kn": 5.0, "cog_deg": 90.0, "heading_true_deg": 90.0}, "gps")
    for _ in range(10):
        h.update({"tws_kn": 12.0, "twa_deg": 0.0}, "mwv-t")   # instrument true wind
        c.t += 0.5
        h.update({"aws_kn": 17.0, "awa_deg": 0.0}, "mwv-r")   # apparent wind must not re-derive
        c.t += 0.5
    rows = {m: v for _, m, v, _ in h.flush_minute()}
    assert rows["tws_kn"] == pytest.approx(12.0)
    assert rows["twd_deg"] == pytest.approx(90.0)


def test_sea_level_reduction_applied_to_barometer():
    s = Settings(sensors={"pressure_altitude_m": 176, "pressure_offset_hpa": 0.5})
    h = SensorHub(s, clock=Clock())
    h.update({"pressure_hpa": 994.0}, "baro")
    assert h.value("pressure_station_hpa") == pytest.approx(994.5)
    assert h.value("pressure_hpa") == pytest.approx(1015.3, abs=0.4)


def test_barometer_garbage_rejected():
    h, _ = hub()
    h.update({"pressure_hpa": 3000.0}, "xdr")  # e.g. an oil-pressure transducer in bar*1000
    assert h.value("pressure_hpa") is None


def test_xdr_ignores_engine_and_fridge():
    d = parse(with_checksum("YDXDR,C,85.0,C,ENGT#0,C,4.0,C,FRIDGE,P,3.2,B,ENGOILP"))
    assert d == {}
    d = parse(with_checksum("YDXDR,C,21.0,C,ENV_OUTAIR_T,C,24.0,C,ENV_INAIR_T"))
    assert d == {"air_temp_c": 21.0, "cabin_temp_c": 24.0}


def test_truncated_sentence_without_checksum_rejected():
    assert parse("$IIMWV,045.0,R,1") == {}
    assert parse("$IIMWV,045.0,R,12.5,N,A") == {"awa_deg": 45.0, "aws_kn": 12.5}


def test_udp_listens_on_all_interfaces_by_default():
    assert source_name(NmeaSource(type="udp", port=10110)) == "nmea-udp://0.0.0.0:10110"
    assert source_name(NmeaSource(type="tcp")) == "nmea-tcp://127.0.0.1:10110"


def test_antimeridian_grid_box():
    pts = make_grid(-17.0, 179.9, 2, 0.25)
    box = bbox_of(pts, 179.9)
    assert box.max_lon - box.min_lon == pytest.approx(1.0)
    assert box.contains(-17.0, 179.9) and box.contains(-17.0, -179.8)
    assert box.edge_distance_deg(-17.0, 179.9) > 0.3
    assert not box.contains(-17.0, 0.0)


def test_passage_grid_covers_radius_east_west_at_high_latitude():
    pts, spacing = grid_for_radius(60.0, 5.0, 150, max_points=400)
    east = max(p[1] for p in pts)
    assert distance_nm(60.0, 5.0, 60.0, east) >= 140
    assert len(pts) <= 400
