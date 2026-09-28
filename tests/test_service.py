"""End-to-end behaviour of the service using the demo transport (real parsers, fake network)."""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from shweather.app import create_app
from shweather.config import BandwidthConfig, Position, Settings
from shweather.demo import DemoHandler, Scenario
from shweather.net import BudgetExceeded
from shweather.scheduler import Scheduler
from shweather.service import NoPosition, WeatherService

LAKE_MICHIGAN = (41.90, -87.50)
CAPE_COD = (41.45, -70.10)
ENGLISH_CHANNEL = (50.10, -3.00)


class Switchable(httpx.AsyncBaseTransport):
    """Demo transport that can be switched off to simulate losing the connection at sea."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.online = True
        self.requests: list[str] = []

    async def handle_async_request(self, request):
        self.requests.append(str(request.url))
        if not self.online:
            raise httpx.ConnectError("network unreachable", request=request)
        return await self.inner.handle_async_request(request)


def make_service(tmp_path, where=LAKE_MICHIGAN, **settings):
    st = Settings(data_dir=tmp_path, home=Position(lat=where[0], lon=where[1]), **settings)
    scenario = Scenario(time.time(), *where)
    transport = Switchable(httpx.MockTransport(DemoHandler(scenario, st.home)))
    return WeatherService(st, transport=transport), transport


@pytest.mark.anyio
async def test_great_lakes_waves_come_from_nws(tmp_path):
    svc, _ = make_service(tmp_path)
    result = await svc.refresh_all()
    assert result["forecast"]["points"] == 25
    fc = svc.forecast_at(*LAKE_MICHIGAN)
    assert set(fc["filled_from_nws"]) >= {"wave_height_m", "wave_period_s"}
    assert any(v is not None for v in fc["series"]["wave_height_m"])
    assert svc.status["coops"]["applicable"] is False  # no tides on the lakes
    assert svc.cached("marine_text")["data"]["zone"] == "LMZ741"
    await svc.aclose()


@pytest.mark.anyio
async def test_ocean_uses_open_meteo_marine_and_tides(tmp_path):
    svc, _ = make_service(tmp_path, where=CAPE_COD)
    await svc.refresh_all()
    fc = svc.forecast_at(*CAPE_COD)
    assert fc["filled_from_nws"] == []
    assert "open_meteo_marine" in fc["run"]["sources"]
    assert any(v for v in fc["series"]["swell_height_m"])
    tides = svc.cached("tides")["data"]
    assert tides["tide"]["hilo"] and tides["current"]["events"]
    await svc.aclose()


@pytest.mark.anyio
async def test_outside_us_nws_is_not_applicable_not_an_error(tmp_path):
    svc, _ = make_service(tmp_path, where=ENGLISH_CHANNEL)
    await svc.refresh_all()
    assert svc.status["open_meteo"].get("error") is None
    assert svc.status["nws_grid"]["applicable"] is False and "error" not in svc.status["nws_grid"]
    assert svc.now()["wind"]["source"] == "forecast"
    await svc.aclose()


@pytest.mark.anyio
async def test_keeps_working_offline(tmp_path):
    svc, net = make_service(tmp_path)
    await svc.refresh_all()
    net.online = False
    with pytest.raises(httpx.ConnectError):
        await svc.refresh_forecast()
    assert "network unreachable" in svc.status["open_meteo"]["error"]
    now = svc.now()  # served entirely from local state
    assert now["wind"]["value"] > 0 and now["forecast"]["run"]["age_s"] >= 0
    assert now["alerts"] and now["alerts"][0]["event"] == "Small Craft Advisory"
    view = svc.forecast_view(None, None, hours=48)
    assert len(view["series"]["time"]) >= 48
    await svc.aclose()


@pytest.mark.anyio
async def test_scheduler_backs_off_when_offline(tmp_path):
    svc, net = make_service(tmp_path)
    net.online = False
    sched = Scheduler(svc)
    now = svc.clock()
    await sched.run_forecast_if_due(now)
    assert sched.forecast_failures == 1 and sched.forecast_next_attempt == pytest.approx(now + 300, abs=1)
    before = len(net.requests)
    await sched.run_forecast_if_due(now + 60)  # still backing off: no new request
    assert len(net.requests) == before
    await svc.aclose()


@pytest.mark.anyio
async def test_forecast_follows_the_boat(tmp_path):
    svc, _ = make_service(tmp_path)
    await svc.refresh_forecast()
    assert svc.forecast_due() == (False, "fresh")
    # 12 nm north: still inside the grid, but far enough from the centre to refresh.
    svc.set_position(LAKE_MICHIGAN[0] + 0.2, LAKE_MICHIGAN[1])
    due, why = svc.forecast_due()
    assert due and ("centre" in why or "edge" in why)
    # Interpolated forecast still available there while offline.
    assert svc.forecast_at(LAKE_MICHIGAN[0] + 0.2, LAKE_MICHIGAN[1])["run"]["inside_grid"]
    svc.set_position(LAKE_MICHIGAN[0] + 2.0, LAKE_MICHIGAN[1])
    assert svc.forecast_due() == (True, "boat has left the forecast grid")
    await svc.aclose()


@pytest.mark.anyio
async def test_passage_download_is_wider_and_capped(tmp_path):
    svc, _ = make_service(tmp_path, where=CAPE_COD)
    res = await svc.refresh_forecast(radius_nm=150)
    assert res["kind"] == "passage" and res["points"] <= 400 and res["spacing_deg"] > 0.25
    await svc.aclose()


@pytest.mark.anyio
async def test_saver_mode_shrinks_downloads(tmp_path):
    svc, net = make_service(tmp_path, where=CAPE_COD)
    await svc.refresh_forecast()
    normal = len(net.requests)
    svc.set_bandwidth(BandwidthConfig(saver=True))
    net.requests.clear()
    res = await svc.refresh_forecast()
    assert res["points"] == 9
    assert len(net.requests) < normal  # skipped the NWS gridpoint: ocean waves already available
    fc = svc.forecast_at(*CAPE_COD)
    assert len(fc["series"]["time"]) == (1 + 3) * 24
    assert svc.refresh_interval_s() == 3 * svc.settings.forecast.refresh_minutes * 60
    await svc.aclose()


@pytest.mark.anyio
async def test_budget_reserve_still_delivers_alerts(tmp_path):
    svc, _ = make_service(tmp_path, bandwidth=BandwidthConfig(daily_mb=1, alert_reserve_pct=10))
    svc.db.add_net_bytes(950_000, svc.http.today())  # 95% used: inside the reserve
    result = await svc.refresh_all()
    assert "data cap reached" in result["forecast"]["error"]
    assert svc.status["nws_alerts"].get("error") is None
    assert svc.cached("alerts")["data"]["alerts"][0]["event"] == "Small Craft Advisory"
    with pytest.raises(BudgetExceeded):
        await svc.refresh_forecast()
    await svc.aclose()


def test_no_position_is_explicit(tmp_path):
    svc = WeatherService(Settings(data_dir=tmp_path))
    with pytest.raises(NoPosition):
        svc.position()


W = {"X-SHW-Client": "test"}  # write endpoints require this header (CSRF guard)


def test_api_end_to_end(tmp_path):
    st = Settings(data_dir=tmp_path, demo=True)
    with TestClient(create_app(st, start_background=False), headers=W) as c:
        assert c.post("/api/refresh").status_code == 200
        for path in ("/api/now", "/api/forecast?hours=24", "/api/status", "/api/alerts", "/api/marine-text",
                     "/api/buoys", "/api/tides", "/api/observations?hours=1", "/api/boat", "/api/bandwidth",
                     "/", "/docs", "/openapi.json"):
            assert c.get(path).status_code == 200, path
        f = c.get("/api/forecast?hours=24&past_hours=0").json()
        assert len(f["series"]["time"]) == len(f["assessment"]) >= 24

        bw = {"max_kbps": 64, "daily_mb": 25, "monthly_mb": 500, "alert_reserve_pct": 15, "saver": True}
        assert c.put("/api/bandwidth", json=bw).json() == bw
        view = c.get("/api/bandwidth").json()
        assert view["settings"]["saver"] is True and view["usage"]["today"]["cap_mb"] == 25
        assert c.get("/api/status").json()["bandwidth"]["max_kbps"] == 64
        assert c.put("/api/bandwidth", json={"max_kbps": -5}).status_code == 422

        boat = c.get("/api/boat").json() | {"max_wind_kn": 18}
        assert c.put("/api/boat", json=boat).json()["max_wind_kn"] == 18

        assert c.post("/api/position", json={"lat": 95, "lon": 0}).status_code == 422
        pos = c.post("/api/position", json={"lat": 42.0, "lon": -87.6}).json()
        assert pos["source"] == "phone"


def test_write_endpoints_need_client_header_and_token(tmp_path):
    st = Settings(data_dir=tmp_path, demo=True, api_token="s3cret")
    with TestClient(create_app(st, start_background=False)) as c:
        body = {"lat": 42.0, "lon": -87.6}
        assert c.post("/api/position", json=body).status_code == 403           # no custom header
        assert c.post("/api/position", json=body, headers=W).status_code == 401  # no token
        bad = W | {"X-SHW-Token": "nope"}
        assert c.post("/api/position", json=body, headers=bad).status_code == 401
        ok = W | {"Authorization": "Bearer s3cret"}
        assert c.post("/api/position", json=body, headers=ok).status_code == 200
        assert c.get("/api/boat").status_code == 200                           # reads stay open


@pytest.mark.anyio
async def test_forecast_pressure_tendency_ignores_local_offset(tmp_path):
    """A barometer that reads high must not turn into a fake 'rising' tendency."""
    svc, _ = make_service(tmp_path, where=CAPE_COD)
    await svc.refresh_forecast()
    fc = svc.forecast_at(*CAPE_COD, localize=False)
    now = svc.clock()
    rows = [(now - i * 60, 1040.0, ) for i in range(6 * 60, 30, -1)]  # 20+ hPa high, stopped 30 min ago
    svc.db.add_observations([(t, "pressure_hpa", v, "test") for t, v in rows])
    svc._interp_cache.clear()
    tend = svc.now()["tendency"]
    import shweather.series as S
    raw_change = S.value_at(fc["series"], "pressure_hpa", now) - S.value_at(fc["series"], "pressure_hpa", now - 3 * 3600)
    assert tend["source"] == "forecast"
    assert tend["change_hpa"] == pytest.approx(raw_change, abs=0.2)
    await svc.aclose()
