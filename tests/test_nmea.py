import pytest

from shweather.sensors.nmea0183 import checksum_ok, parse, with_checksum


def test_checksum_roundtrip():
    s = with_checksum("WIMWV,045.0,R,12.5,N,A")
    assert checksum_ok(s)
    assert not checksum_ok(s[:-2] + "00")


def test_bad_checksum_is_dropped():
    good = with_checksum("WIMWV,045.0,R,12.5,N,A")
    assert parse(good)
    assert parse(good[:-2] + ("00" if good[-2:] != "00" else "01")) == {}


def test_rmc_position_speed_variation():
    s = with_checksum("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W")
    d = parse(s)
    assert d["lat"] == pytest.approx(48.1173, abs=1e-4)
    assert d["lon"] == pytest.approx(11.5167, abs=1e-4)
    assert d["sog_kn"] == 22.4
    assert d["cog_deg"] == 84.4
    assert d["mag_variation_deg"] == -3.1


def test_rmc_void_fix_ignored():
    assert parse(with_checksum("GPRMC,123519,V,4807.038,N,01131.000,E,022.4,084.4,230394,,")) == {}


def test_gga_southern_western():
    d = parse(with_checksum("GPGGA,123519,3351.000,S,15112.000,W,1,08,0.9,5.0,M,,M,,"))
    assert d["lat"] == pytest.approx(-33.85)
    assert d["lon"] == pytest.approx(-151.2)


def test_gga_no_fix_ignored():
    assert parse(with_checksum("GPGGA,123519,3351.000,S,15112.000,W,0,00,,,M,,M,,")) == {}


@pytest.mark.parametrize("unit,expected", [("N", 10.0), ("M", 10 / 0.514444), ("K", 10 / 1.852)])
def test_mwv_units(unit, expected):
    d = parse(with_checksum(f"IIMWV,030,R,10,{unit},A"))
    assert d["awa_deg"] == 30
    assert d["aws_kn"] == pytest.approx(expected, rel=1e-3)


def test_mwv_true_and_invalid():
    assert parse(with_checksum("IIMWV,270,T,8,N,A")) == {"twa_deg": 270, "tws_kn": 8}
    assert parse(with_checksum("IIMWV,270,T,8,N,V")) == {}


def test_vwr_port_side_becomes_360_minus():
    d = parse(with_checksum("IIVWR,40,L,12,N,,,,"))
    assert d["awa_deg"] == 320
    assert d["aws_kn"] == 12


def test_mda_full():
    d = parse(with_checksum("WIMDA,30.012,I,1.0163,B,21.5,C,18.2,C,65.0,,12.1,C,245.0,T,250.0,M,12.5,N,6.4,M"))
    assert d["pressure_hpa"] == pytest.approx(1016.3)
    assert d["air_temp_c"] == 21.5
    assert d["water_temp_c"] == 18.2
    assert d["humidity_pct"] == 65.0
    assert d["dewpoint_c"] == 12.1
    assert d["twd_deg"] == 245.0
    assert d["tws_kn"] == 12.5


def test_mda_inhg_only():
    d = parse(with_checksum("WIMDA,30.000,I,,B,,C,,C,,,,C,,T,,M,,N,,M"))
    assert d["pressure_hpa"] == pytest.approx(1015.9, abs=0.1)


def test_xdr_pressure_temp_humidity():
    d = parse(with_checksum("IIXDR,P,1.0121,B,Barometer,C,19.5,C,AirTemp,H,55,P,Humidity,C,17.0,C,WaterTemp"))
    assert d["pressure_hpa"] == pytest.approx(1012.1)
    assert d["air_temp_c"] == 19.5
    assert d["humidity_pct"] == 55
    assert d["water_temp_c"] == 17.0


def test_xdr_pascal():
    assert parse(with_checksum("IIXDR,P,101325,P,BARO"))["pressure_hpa"] == pytest.approx(1013.25)


def test_hdg_applies_deviation_and_variation():
    d = parse(with_checksum("IIHDG,100.0,2.0,W,5.0,E"))
    assert d["heading_mag_deg"] == 98.0
    assert d["heading_true_deg"] == 103.0


def test_vhw_and_mwd():
    assert parse(with_checksum("IIVHW,045.0,T,041.0,M,6.2,N,11.5,K"))["stw_kn"] == 6.2
    d = parse(with_checksum("WIMWD,270.0,T,266.0,M,15.0,N,7.7,M"))
    assert d == {"twd_deg": 270.0, "tws_kn": 15.0}


def test_ignores_ais_proprietary_and_garbage():
    assert parse("!AIVDM,1,1,,A,15M67FC000G?ufbE`FepT@3n00Sa,0*5C") == {}
    assert parse(with_checksum("PGRME,15.0,M,45.0,M,25.0,M")) == {}
    assert parse("hello") == {}
    assert parse(with_checksum("IIMWV,abc,R,,N,A")) == {}
