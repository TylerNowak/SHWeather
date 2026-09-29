"""Radar tab: imagery frame lists, the tile cache and its guards, map fields, text-only gzip."""

import asyncio
import os
import struct
import time
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient

from shweather.app import create_app
from shweather.config import BandwidthConfig, ImageryConfig, Position, Settings
from shweather.demo import DemoHandler, Scenario
from shweather.demo_imagery import DemoSky, radar_tile
from shweather.imagery import (
    MAX_TILE_DISTANCE_NM,
    Frame,
    FrameList,
    ImageryError,
    distance_to_tile_nm,
    fill,
    pick_satellite,
    tile_at,
    tile_bounds,
)
from shweather.service import WeatherService

LAKE_MICHIGAN = (41.90, -87.50)
W = {"X-SHW-Client": "test"}


class Recording(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner = inner
        self.online = True
        self.urls: list[str] = []

    async def handle_async_request(self, request):
        self.urls.append(str(request.url))
        if not self.online:
            raise httpx.ConnectError("network unreachable", request=request)
        return await self.inner.handle_async_request(request)


def make(tmp_path, where=LAKE_MICHIGAN, **settings):
    st = Settings(data_dir=tmp_path, home=Position(lat=where[0], lon=where[1]), **settings)
    transport = Recording(httpx.MockTransport(DemoHandler(Scenario(time.time(), *where), st.home)))
    return WeatherService(st, transport=transport), transport


def boat_tile(z=7):
    return tile_at(*LAKE_MICHIGAN, z)


def test_tile_maths():
    x, y = tile_at(41.9, -87.5, 7)
    s, w, n, e = tile_bounds(7, x, y)
    assert s <= 41.9 <= n and w <= -87.5 <= e
    assert distance_to_tile_nm(41.9, -87.5, 7, x, y) == 0
    # across the antimeridian: the tile just east of 180 is next to a boat at 179.9 E
    assert distance_to_tile_nm(0.0, 179.9, 6, 0, 32) < 10
    assert pick_satellite(-87.5)[0].startswith("GOES-East")
    assert pick_satellite(-150.0)[0].startswith("GOES-West")
    assert pick_satellite(145.0)[0].startswith("Himawari")
    assert pick_satellite(15.0) is None               # Europe: no GIBS geostationary layer


@pytest.mark.anyio
async def test_radar_frames_and_tile_cache(tmp_path):
    svc, net = make(tmp_path)
    view = await svc.imagery.view(may_fetch=True)
    radar = view["layers"]["radar"]
    assert radar["error"] is None and radar["max_zoom"] == 7 and radar["attribution"]
    times = [f["time"] for f in radar["frames"]]
    assert len(times) == 7 and all(b - a == 600 for a, b in zip(times, times[1:], strict=False))

    x, y = boat_tile()
    data = await svc.imagery.tile("radar", times[-1], 7, x, y, may_fetch=True)
    assert data.startswith(b"\x89PNG")
    net.online = False                                 # cached tiles need no network
    assert await svc.imagery.tile("radar", times[-1], 7, x, y, may_fetch=False) == data
    await svc.aclose()


@pytest.mark.anyio
async def test_tile_guards(tmp_path):
    svc, _ = make(tmp_path)
    frames = (await svc.imagery.view(may_fetch=True))["layers"]["radar"]["frames"]
    t = frames[-1]["time"]
    x, y = boat_tile()
    with pytest.raises(ImageryError) as e:            # a download needs the app's permission
        await svc.imagery.tile("radar", t, 7, x, y, may_fetch=False)
    assert e.value.status == 404
    with pytest.raises(ImageryError) as e:            # beyond the source's zoom
        await svc.imagery.tile("radar", t, 8, *boat_tile(8), may_fetch=True)
    assert e.value.status == 404
    with pytest.raises(ImageryError) as e:            # far from the boat
        await svc.imagery.tile("radar", t, 7, 10, 10, may_fetch=True)
    assert e.value.status == 403 and str(MAX_TILE_DISTANCE_NM) in str(e.value)
    with pytest.raises(ImageryError) as e:            # a frame the source no longer lists
        await svc.imagery.tile("radar", t - 86400, 7, x, y, may_fetch=True)
    assert e.value.status == 404
    with pytest.raises(ImageryError):
        await svc.imagery.tile("nope", t, 7, x, y, may_fetch=True)
    await svc.aclose()


@pytest.mark.anyio
async def test_same_tile_for_two_phones_is_downloaded_once(tmp_path):
    svc, net = make(tmp_path)
    t = (await svc.imagery.view(may_fetch=True))["layers"]["radar"]["frames"][-1]["time"]
    x, y = boat_tile()
    a, b = await asyncio.gather(svc.imagery.tile("radar", t, 7, x, y, may_fetch=True),
                                svc.imagery.tile("radar", t, 7, x, y, may_fetch=True))
    assert a == b
    assert sum(f"/{t}/256/7/{x}/{y}/" in u for u in net.urls) == 1
    await svc.aclose()


@pytest.mark.anyio
async def test_satellite_finds_latest_image_and_tile_matrix(tmp_path):
    svc, _ = make(tmp_path)
    sat = (await svc.imagery.view(may_fetch=True))["layers"]["satellite"]
    assert sat["error"] is None and sat["max_zoom"] == 6 and "GOES-East" in sat["attribution"]
    latest = sat["frames"][-1]["time"]
    # The demo, like GIBS, publishes images ~30 min late; the probe must not claim newer ones.
    assert time.time() - latest >= DemoHandler.GIBS_LATENCY_S - 1
    assert svc.db.kv_get("gibs_level:GOES-East_ABI_Band13_Clean_Infrared")[0] == 6
    x, y = tile_at(*LAKE_MICHIGAN, 6)
    assert (await svc.imagery.tile("satellite", latest, 6, x, y, may_fetch=False)).startswith(b"\x89PNG")  # probe cached it
    await svc.aclose()


@pytest.mark.anyio
async def test_saver_mode_offers_the_latest_frame_only(tmp_path):
    svc, _ = make(tmp_path)
    svc.set_bandwidth(BandwidthConfig(saver=True))
    view = await svc.imagery.view(may_fetch=True)
    assert view["saver"] is True
    assert len(view["layers"]["radar"]["frames"]) == 1 and len(view["layers"]["satellite"]["frames"]) == 1
    await svc.aclose()


@pytest.mark.anyio
async def test_data_cap_pauses_imagery_but_keeps_cached_tiles(tmp_path):
    svc, _ = make(tmp_path)
    t = (await svc.imagery.view(may_fetch=True))["layers"]["radar"]["frames"][-1]["time"]
    x, y = boat_tile()
    cached = await svc.imagery.tile("radar", t, 7, x, y, may_fetch=True)
    svc.set_bandwidth(BandwidthConfig(daily_mb=0.001))            # already used more than this
    view = await svc.imagery.view(may_fetch=True)
    assert "cap reached" in view["paused"]
    assert view["layers"]["radar"]["frames"]                     # the frames we have stay listed
    assert await svc.imagery.tile("radar", t, 7, x, y, may_fetch=True) == cached
    with pytest.raises(ImageryError) as e:
        await svc.imagery.tile("radar", t, 7, x + 1, y, may_fetch=True)
    assert e.value.status == 503 and "paused" in str(e.value)
    await svc.aclose()


@pytest.mark.anyio
async def test_offline_keeps_last_frame_list(tmp_path):
    svc, net = make(tmp_path)
    frames = (await svc.imagery.view(may_fetch=True))["layers"]["radar"]["frames"]
    net.online = False
    svc.imagery._lists["radar"].fetched_at -= 3600            # due for a refresh
    view = await svc.imagery.view(may_fetch=True)
    assert view["layers"]["radar"]["frames"] == frames
    assert "no connection" in view["layers"]["radar"]["warning"]
    x, y = boat_tile()
    with pytest.raises(ImageryError) as e:
        await svc.imagery.tile("radar", frames[-1]["time"], 7, x, y, may_fetch=True)
    assert e.value.status == 503
    await svc.aclose()


@pytest.mark.anyio
async def test_iem_radar_and_disabled_layers(tmp_path):
    svc, net = make(tmp_path, imagery=ImageryConfig(radar="iem", satellite="none"))
    view = await svc.imagery.view(may_fetch=True)
    assert set(view["layers"]) == {"radar"}
    frames = svc.imagery._lists["radar"].frames
    assert len(frames) == 7 and time.time() - frames[-1].time >= 600
    assert "ridge::USCOMP-N0Q-" in frames[-1].url and frames[-1].url.endswith("/{z}/{x}/{y}.png")
    assert not net.urls                                        # IEM frames are listed without a download
    await svc.aclose()

    svc, _ = make(tmp_path, imagery=ImageryConfig(enabled=False))
    assert (await svc.imagery.view(may_fetch=True))["layers"] == {}
    with pytest.raises(ImageryError):
        await svc.imagery.tile("radar", 0, 7, 0, 0, may_fetch=True)
    await svc.aclose()


@pytest.mark.anyio
async def test_prune_removes_old_frames_and_keeps_under_the_cap(tmp_path):
    svc, _ = make(tmp_path, imagery=ImageryConfig(cache_mb=10))
    old = svc.imagery._path("radar", int(time.time()) - 7 * 3600, 7, 1, 1)
    new = svc.imagery._path("radar", int(time.time()), 7, 1, 1)
    for p in (old, new):
        svc.imagery._store(p, b"\x89PNG" + b"0" * 100)
    svc.imagery.prune()
    assert not old.exists() and new.exists()
    big = [svc.imagery._path("basemap", 0, 9, i, 1) for i in range(3)]
    for i, p in enumerate(big):
        svc.imagery._store(p, b"\x89PNG" + b"0" * 4_500_000)
        os.utime(p, (1000 + i, 1000 + i))
    svc.imagery.prune()
    assert not big[0].exists() and big[2].exists()             # oldest went first
    await svc.aclose()


def test_demo_radar_tile_is_a_valid_png():
    t0 = time.time()
    data = radar_tile(DemoSky(t0, *LAKE_MICHIGAN, t0 + 9 * 3600), t0, 7, *boat_tile())
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, color = struct.unpack(">IIBB", data[16:26])
    assert (width, height, depth, color) == (256, 256, 8, 6)
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    assert len(zlib.decompress(idat)) == 256 * (1 + 256 * 4)


def test_api_map_imagery_tiles_and_gzip(tmp_path):
    st = Settings(data_dir=tmp_path, demo=True)
    with TestClient(create_app(st, start_background=False), headers=W) as c:
        assert c.get("/api/map").status_code == 404                  # nothing downloaded yet
        c.post("/api/refresh")
        m = c.get("/api/map?past_hours=0&hours=12")
        assert m.status_code == 200 and m.headers["content-encoding"] == "gzip"
        body = m.json()
        assert len(body["points"]) == 25 and 12 <= len(body["time"]) <= 13
        assert set(body["fields"]) == {"wind_speed_kn", "wind_gust_kn", "wind_dir_deg", "cloud_pct", "precip_mm"}
        assert all(len(series) == len(body["time"]) for series in body["fields"]["wind_speed_kn"])

        c.post("/api/refresh?radius_nm=60")                         # a passage grid is wider: the map uses it
        wide = c.get("/api/map").json()
        assert len(wide["points"]) > 25 and wide["run"]["kind"] == "passage"

        view = c.get("/api/imagery").json()
        t = view["layers"]["radar"]["frames"][-1]["time"]
        x, y = boat_tile()
        url = f"/api/tiles/radar/{t}/7/{x}/{y}.png"
        assert c.get(url, headers={"X-SHW-Client": ""}).status_code == 404   # others can't trigger downloads
        tile = c.get(url)
        assert tile.status_code == 200 and tile.headers["content-type"] == "image/png"
        assert "content-encoding" not in tile.headers and "max-age" in tile.headers["cache-control"]
        assert c.get(url, headers={"X-SHW-Client": ""}).status_code == 200   # ...but may read the cache
        assert c.get(f"/api/tiles/radar/{t}/7/5/5.png").status_code == 403

        basemap = c.get("/data/basemap.json")
        assert basemap.status_code == 200 and basemap.headers["content-encoding"] == "gzip"
        assert basemap.json()["q"] == 100



# ---------------------------------------------------------------- regressions from review


def test_huge_zoom_is_refused_immediately(tmp_path):
    st = Settings(data_dir=tmp_path, demo=True)
    with TestClient(create_app(st, start_background=False), headers=W) as c:
        t0 = time.monotonic()
        assert c.get("/api/tiles/radar/0/1000000000/0/0.png").status_code == 422
        assert c.get("/api/tiles/radar/-5/7/0/0.png").status_code == 422
        assert time.monotonic() - t0 < 1


@pytest.mark.anyio
async def test_huge_zoom_is_refused_before_any_maths(tmp_path):
    svc, _ = make(tmp_path)
    t0 = time.monotonic()
    with pytest.raises(ImageryError):
        await svc.imagery.tile("radar", 0, 10 ** 9, 0, 0, may_fetch=True)
    assert time.monotonic() - t0 < 0.1
    await svc.aclose()


@pytest.mark.anyio
async def test_five_minute_sources_keep_their_loop_between_refreshes(tmp_path):
    # Frames on 10-minute marks only: a refresh 5 minutes later must reuse the loop, not
    # shift every frame (which would re-download all of it).
    svc, _ = make(tmp_path, imagery=ImageryConfig(radar="iem"))
    t0 = 1_790_000_000
    a = {f.time for f in (await svc.imagery._list_radar(t0)).frames}
    b = {f.time for f in (await svc.imagery._list_radar(t0 + 300)).frames}
    c = {f.time for f in (await svc.imagery._list_radar(t0 + 600)).frames}
    assert all(t % 600 == 0 for t in a | b | c)
    assert len(a & b) >= 6 and len(a & c) >= 6
    # The loop is as long whether or not the newest frame falls on a mark.
    assert len(a) == len(b) == len(c) == 7
    await svc.aclose()


class SlowTiles(httpx.AsyncBaseTransport):
    """Demo transport, with tile downloads taking a moment."""

    def __init__(self, inner):
        self.inner = inner
        self.tile_requests = 0

    async def handle_async_request(self, request):
        if "/256/" in request.url.path:
            self.tile_requests += 1
            await asyncio.sleep(0.2)
        return await self.inner.handle_async_request(request)


@pytest.mark.anyio
async def test_one_phone_giving_up_does_not_cancel_the_download_for_others(tmp_path):
    st = Settings(data_dir=tmp_path, home=Position(lat=LAKE_MICHIGAN[0], lon=LAKE_MICHIGAN[1]))
    net = SlowTiles(httpx.MockTransport(DemoHandler(Scenario(time.time(), *LAKE_MICHIGAN), st.home)))
    svc = WeatherService(st, transport=net)
    t = (await svc.imagery.view(may_fetch=True))["layers"]["radar"]["frames"][-1]["time"]
    x, y = boat_tile()
    first = asyncio.create_task(svc.imagery.tile("radar", t, 7, x, y, may_fetch=True))
    second = asyncio.create_task(svc.imagery.tile("radar", t, 7, x, y, may_fetch=True))
    await asyncio.sleep(0.05)
    first.cancel()
    assert (await second).startswith(b"\x89PNG")
    assert net.tile_requests == 1
    await svc.aclose()


@pytest.mark.anyio
async def test_unexpected_frame_list_is_an_error_not_a_crash(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"host": "https://t.example", "radar": {"past": [{"path": "/v2/radar/1"}]}})

    st = Settings(data_dir=tmp_path, home=Position(lat=41.9, lon=-87.5), imagery=ImageryConfig(satellite="none"))
    svc = WeatherService(st, transport=httpx.MockTransport(handler))
    view = await svc.imagery.view(may_fetch=True)
    assert "unexpected" in view["layers"]["radar"]["error"]
    await svc.aclose()


@pytest.mark.anyio
async def test_old_frames_are_not_offered(tmp_path):
    svc, _ = make(tmp_path)
    await svc.imagery.view(may_fetch=True)
    old = int(time.time()) - 7 * 3600
    svc.imagery._lists["radar"] = FrameList([Frame(old, "https://x/{z}/{x}/{y}")], 7, "rainviewer", "RV",
                                            fetched_at=time.time())
    assert (await svc.imagery.view(may_fetch=False))["layers"]["radar"]["frames"] == []
    await svc.aclose()


@pytest.mark.anyio
async def test_cache_is_kept_per_source(tmp_path):
    svc, _ = make(tmp_path)
    rv = svc.imagery._path("radar", 100, 7, 1, 2)
    svc.settings.imagery = ImageryConfig(radar="iem")
    assert svc.imagery._path("radar", 100, 7, 1, 2) != rv
    assert "GOES-East" in str(svc.imagery._path("satellite", 100, 6, 1, 2))
    assert fill("https://{s}.tiles.example/{z}/{x}/{y}{r}.png", 3, 4, 5) == "https://a.tiles.example/3/4/5.png"
    await svc.aclose()


@pytest.mark.anyio
async def test_prune_survives_files_it_cannot_delete(tmp_path, monkeypatch):
    svc, _ = make(tmp_path, imagery=ImageryConfig(cache_mb=10))
    for i in range(3):
        svc.imagery._store(svc.imagery._path("basemap", 0, 9, i, 1), b"\x89PNG" + b"0" * 4_500_000)

    def locked(self, missing_ok=False):
        raise PermissionError("in use by another process")

    monkeypatch.setattr(type(tmp_path), "unlink", locked)
    svc.imagery.prune()                                   # no exception
    await svc.aclose()


def test_radar_map_prefers_the_newest_forecast(tmp_path):
    st = Settings(data_dir=tmp_path, demo=True)
    with TestClient(create_app(st, start_background=False), headers=W) as c:
        c.post("/api/refresh?radius_nm=60")               # a wide passage grid...
        svc = c.app.state.service
        svc.db.execute("UPDATE forecast_runs SET fetched_at = fetched_at - 5 * 3600")
        c.post("/api/refresh")                            # ...then a fresh automatic grid
        assert c.get("/api/map").json()["run"]["kind"] == "auto"


@pytest.mark.anyio
async def test_satellite_loop_uses_fixed_20_minute_marks(tmp_path):
    svc, _ = make(tmp_path)
    frames = (await svc.imagery.view(may_fetch=True))["layers"]["satellite"]["frames"]
    times = [f["time"] for f in frames]
    assert 3 <= len(times) <= 4                                   # an hour at 20-minute marks
    assert all(t % 1200 == 0 for t in times)
    # A refresh 10 minutes later (one new image) keeps the older marks, so they aren't re-downloaded.
    from shweather.imagery import Frame
    later = [Frame(t, "u") for t in range(times[-1] - 3600, times[-1] + 601, 600)]
    again = [f.time for f in svc.imagery._trim(later, 1200)]
    assert len(set(times) & set(again)) >= 3
    await svc.aclose()
