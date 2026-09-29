"""Shared HTTP client: identifying User-Agent, bandwidth limiting and data-cap accounting.

Bandwidth controls (see BandwidthConfig):

- **Speed cap** (``max_kbps``): responses are streamed and read no faster than the cap,
  through one token bucket shared by all concurrent fetches, so weather pulls never
  saturate a slow cellular or satellite link that the crew is also using. TCP flow control
  then slows the sender. The OS socket buffer can absorb a short initial burst, so treat
  the cap as an average, not a hard ceiling.
- **Data caps** (``daily_mb``, ``monthly_mb``): wire bytes (after compression) are counted
  per UTC day; requests are refused once a cap is reached.
- **Alert reserve** (``alert_reserve_pct``): ordinary fetches stop early, keeping the last
  slice of each cap for requests marked ``essential`` (NWS alerts, a few KB each), so a
  used-up budget never hides a gale warning. Ordinary downloads are streamed and aborted
  the moment they would cross into the reserve, so one large response cannot eat it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable

import httpx

from . import __version__
from .config import BandwidthConfig, Settings
from .db import Database

log = logging.getLogger(__name__)

CHUNK = 4096
BURST_S = 0.25


class BudgetExceeded(RuntimeError):
    """A data cap (bandwidth.daily_mb / monthly_mb) has been reached."""


class RateLimiter:
    """Token bucket shared by every download so the *total* rate respects the cap."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._paid_until = 0.0

    async def consume(self, nbytes: int, rate_bytes_s: float) -> None:
        if nbytes <= 0 or rate_bytes_s <= 0:
            return
        now = self.clock()
        self._paid_until = max(self._paid_until, now - BURST_S) + nbytes / rate_bytes_s
        delay = self._paid_until - now
        if delay > 0:
            await asyncio.sleep(delay)


def wire_bytes(resp: httpx.Response, decoded: int | None = None) -> int:
    """Bytes received on the wire (compressed). Falls back to the body size for responses
    that were never streamed from a socket (e.g. test transports)."""
    if resp.num_bytes_downloaded:
        return resp.num_bytes_downloaded
    if decoded is not None:
        return decoded
    return int(resp.headers.get("content-length") or len(resp.content))


class Http:
    def __init__(self, settings: Settings, db: Database, transport: httpx.AsyncBaseTransport | None = None,
                 limits: Callable[[], BandwidthConfig] | None = None):
        self.settings = settings
        self.db = db
        self.limits = limits or (lambda: settings.bandwidth)
        self.limiter = RateLimiter()
        # Downloads still in progress are not in the database yet; count them too, so
        # parallel downloads (e.g. radar tiles) can't each spend the same remaining budget.
        self._pending = 0     # wire bytes of unfinished downloads
        self._consumed = 0    # wire bytes downloaded by this process (only ever grows)
        ua = (f"SHWeather/{__version__} "
              f"(+https://github.com/TylerNowak/SHWeather; {settings.sources.contact})")
        self.client = httpx.AsyncClient(
            timeout=settings.sources.timeout_s,
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True,
            transport=transport,
        )

    # ---- accounting ----

    @staticmethod
    def today() -> str:
        return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")

    def bytes_today(self) -> int:
        return self.db.net_bytes(self.today()) + self._pending

    def bytes_month(self) -> int:
        return self.db.net_bytes_month(self.today()[:7]) + self._pending

    def usage(self) -> dict:
        bw = self.limits()
        day, month = self.bytes_today(), self.bytes_month()

        def cap(used: int, cap_mb: float | None) -> dict:
            if cap_mb is None:
                return {"used_mb": round(used / 1e6, 2), "cap_mb": None, "remaining_mb": None, "pct": None}
            return {"used_mb": round(used / 1e6, 2), "cap_mb": cap_mb,
                    "remaining_mb": round(max(0.0, cap_mb - used / 1e6), 2),
                    "pct": round(100 * used / (cap_mb * 1e6), 1)}

        return {"today": cap(day, bw.daily_mb), "month": cap(month, bw.monthly_mb),
                "max_kbps": bw.max_kbps, "saver": bw.saver, "alert_reserve_pct": bw.alert_reserve_pct}

    def allowance(self, essential: bool = False) -> float | None:
        """Bytes this request may still download (None = unlimited). Raises when none are left."""
        bw = self.limits()
        remaining = None
        for label, cap_mb, used in (("daily", bw.daily_mb, self.bytes_today()),
                                    ("monthly", bw.monthly_mb, self.bytes_month())):
            if cap_mb is None:
                continue
            cap = cap_mb * 1e6
            limit = cap if essential else cap * (1 - bw.alert_reserve_pct / 100.0)
            if used >= limit:
                reserve = "" if essential else " (remaining reserve kept for safety alerts)"
                raise BudgetExceeded(f"{label} data cap reached: {used / 1e6:.2f} of {cap_mb:g} MB{reserve}")
            remaining = limit - used if remaining is None else min(remaining, limit - used)
        return remaining

    def check_budget(self, essential: bool = False) -> None:
        self.allowance(essential)

    # ---- requests ----

    async def get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                  essential: bool = False) -> httpx.Response:
        """GET with caps enforced. ``essential`` requests may use the alert reserve."""
        start = self._consumed        # bytes anyone downloads from now on count against this allowance
        allowance = self.allowance(essential)
        bw = self.limits()
        rate = bw.max_kbps * 1000 / 8 if bw.max_kbps else None
        # Essential requests (tiny alert documents) are never cut off mid-way.
        return await self._stream(url, params, headers, rate, None if essential else allowance, start)

    async def _stream(self, url: str, params: dict | None, headers: dict | None,
                      rate_bytes_s: float | None, allowance: float | None, start: int = 0) -> httpx.Response:
        chunks: list[bytes] = []
        async with self.client.stream("GET", url, params=params, headers=headers) as resp:
            declared = resp.headers.get("content-length")
            if allowance is not None and declared and declared.isdigit() and int(declared) > allowance:
                raise BudgetExceeded(f"response of {int(declared) / 1e6:.2f} MB exceeds the remaining "
                                     f"data budget ({allowance / 1e6:.2f} MB before the alert reserve)")
            seen = decoded = 0
            try:
                async for chunk in resp.aiter_bytes(CHUNK):
                    chunks.append(chunk)
                    decoded += len(chunk)
                    wire = wire_bytes(resp, decoded)
                    delta, seen = wire - seen, wire
                    self._pending += delta
                    self._consumed += delta
                    # Everything downloaded since this request started, by it or by parallel ones.
                    if allowance is not None and self._consumed - start > allowance:
                        raise BudgetExceeded("download stopped: it would have used the data reserved "
                                             "for safety alerts")
                    if rate_bytes_s:
                        await self.limiter.consume(delta, rate_bytes_s)
            finally:
                total = wire_bytes(resp, decoded)
                self._pending -= seen
                self._consumed += max(0, total - seen)
                self.db.add_net_bytes(total, self.today())
            # The body is already decoded, so drop the transfer headers that described the wire form.
            kept = [(k, v) for k, v in resp.headers.multi_items()
                    if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            return httpx.Response(resp.status_code, headers=kept, content=b"".join(chunks), request=resp.request)

    async def aclose(self) -> None:
        await self.client.aclose()
