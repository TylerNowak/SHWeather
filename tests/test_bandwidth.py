import time

import httpx
import pytest

from shweather.config import BandwidthConfig, Settings
from shweather.db import Database
from shweather.net import BudgetExceeded, Http, RateLimiter


class Streamed(httpx.AsyncByteStream):
    """A body delivered in chunks, like a socket, so httpx counts wire bytes."""

    def __init__(self, data: bytes, chunk: int = 8192):
        self.data, self.chunk = data, chunk

    async def __aiter__(self):
        for i in range(0, len(self.data), self.chunk):
            yield self.data[i:i + self.chunk]


def make_http(tmp_path, handler, **bw):
    st = Settings(data_dir=tmp_path, bandwidth=BandwidthConfig(**bw))
    return Http(st, Database(tmp_path / "t.db"), httpx.MockTransport(handler))


def body(n):
    return lambda req: httpx.Response(200, stream=Streamed(b"x" * n))


@pytest.mark.anyio
async def test_user_agent_and_byte_accounting(tmp_path):
    agents = []

    def handler(req):
        agents.append(req.headers["user-agent"])
        return httpx.Response(200, stream=Streamed(b"x" * 1234))

    http = make_http(tmp_path, handler)
    await http.get("https://example.org/a")
    assert "SHWeatherService/" in agents[0] and "admin@example.com" in agents[0]
    assert http.bytes_today() == 1234 and http.bytes_month() == 1234
    await http.aclose()


@pytest.mark.anyio
async def test_daily_cap_keeps_reserve_for_alerts(tmp_path):
    # 1 MB/day with a 10% reserve: ordinary fetches stop at 0.9 MB, alerts may use the rest.
    http = make_http(tmp_path, body(450_000), daily_mb=1, alert_reserve_pct=10)
    await http.get("https://example.org/1")
    await http.get("https://example.org/2")          # 0.90 MB used
    with pytest.raises(BudgetExceeded, match="reserve kept for safety alerts"):
        await http.get("https://example.org/3")
    await http.get("https://api.weather.gov/alerts/active", essential=True)  # 1.35 MB: allowed, cap was not yet hit
    with pytest.raises(BudgetExceeded):
        await http.get("https://api.weather.gov/alerts/active", essential=True)
    u = http.usage()["today"]
    assert u["cap_mb"] == 1 and u["remaining_mb"] == 0 and u["pct"] > 100
    await http.aclose()


@pytest.mark.anyio
async def test_monthly_cap_counts_previous_days(tmp_path):
    http = make_http(tmp_path, body(10), monthly_mb=5)
    month = http.today()[:7]
    http.db.add_net_bytes(3_000_000, f"{month}-01")
    http.db.add_net_bytes(2_000_000, f"{month}-02")
    http.db.add_net_bytes(9_000_000, "1999-12-31")  # other months don't count
    assert http.bytes_month() >= 5_000_000
    with pytest.raises(BudgetExceeded, match="monthly"):
        await http.get("https://example.org/x", essential=True)
    await http.aclose()


@pytest.mark.anyio
async def test_speed_cap_throttles_and_preserves_body(tmp_path):
    payload = b"0123456789" * 6_000  # 60 kB
    http = make_http(tmp_path, lambda r: httpx.Response(200, stream=Streamed(payload),
                                                         headers={"content-type": "text/plain"}),
                     max_kbps=800)  # 100 kB/s -> ~0.6 s minus the 0.25 s burst allowance
    t0 = time.monotonic()
    resp = await http.get("https://example.org/big")
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200 and resp.content == payload and resp.text.startswith("0123")
    assert elapsed >= 0.3
    assert http.bytes_today() == len(payload)
    await http.aclose()


@pytest.mark.anyio
async def test_rate_limiter_is_shared_between_concurrent_downloads():
    import asyncio
    lim = RateLimiter()
    t0 = time.monotonic()
    # Two "downloads" of 50 kB each at 200 kB/s must take ~0.5 s together, not 0.25 s.
    await asyncio.gather(lim.consume(50_000, 200_000), lim.consume(50_000, 200_000))
    assert time.monotonic() - t0 >= 0.2


@pytest.mark.anyio
async def test_preloaded_bodies_are_still_counted(tmp_path):
    http = make_http(tmp_path, lambda r: httpx.Response(200, content=b"y" * 777))
    await http.get("https://example.org/x")
    assert http.bytes_today() == 777
    await http.aclose()


@pytest.mark.anyio
async def test_unlimited_by_default(tmp_path):
    http = make_http(tmp_path, body(5_000_000))
    for _ in range(3):
        await http.get("https://example.org/x")
    assert http.usage()["today"]["cap_mb"] is None
    await http.aclose()


@pytest.mark.anyio
async def test_large_download_cannot_eat_the_alert_reserve(tmp_path):
    # 1 MB cap, 10% reserve, 0.85 MB used: a 300 kB ordinary download must stop at 0.9 MB.
    def handler(req):
        size = 5_000 if "alerts" in str(req.url) else 300_000
        return httpx.Response(200, stream=Streamed(b"z" * size))

    http = make_http(tmp_path, handler, daily_mb=1, alert_reserve_pct=10)
    http.db.add_net_bytes(850_000, http.today())
    with pytest.raises(BudgetExceeded, match="reserved for safety alerts"):
        await http.get("https://example.org/grid")
    assert http.bytes_today() <= 900_000 + 8192           # stopped within one chunk of the line
    resp = await http.get("https://api.weather.gov/alerts/active", essential=True)
    assert resp.status_code == 200 and len(resp.content) == 5_000
    await http.aclose()


@pytest.mark.anyio
async def test_declared_size_over_budget_is_refused_up_front(tmp_path):
    http = make_http(tmp_path, lambda r: httpx.Response(200, content=b"q" * 200_000), daily_mb=1)
    http.db.add_net_bytes(800_000, http.today())          # 100 kB left before the reserve
    with pytest.raises(BudgetExceeded, match="exceeds the remaining"):
        await http.get("https://example.org/big")
    assert http.bytes_today() == 800_000
    await http.aclose()


@pytest.mark.anyio
async def test_parallel_downloads_share_one_budget(tmp_path):
    # 1 MB cap, 10% reserve, 0.897 MB used: 3 kB left for ordinary downloads. Twenty radar
    # tiles of 1.3 kB fetched at once must not each spend those same 3 kB.
    import asyncio

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.01)          # all requests start before any has finished
            yield b"t" * 1300

    http = make_http(tmp_path, lambda r: httpx.Response(200, stream=Slow()), daily_mb=1, alert_reserve_pct=10)
    http.db.add_net_bytes(897_000, http.today())
    results = await asyncio.gather(*(http.get(f"https://tiles.example/{i}.png") for i in range(20)),
                                   return_exceptions=True)
    ok = [r for r in results if isinstance(r, httpx.Response)]
    assert 1 <= len(ok) <= 3
    assert all(isinstance(r, BudgetExceeded) for r in results if not isinstance(r, httpx.Response))
    assert http.bytes_today() <= 1_000_000           # the alert reserve is never used up...
    assert await http.get("https://api.weather.gov/alerts/active", essential=True)   # ...alerts still flow
    await http.aclose()
