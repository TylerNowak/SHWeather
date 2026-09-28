"""Background jobs: fetch when the network allows, persist sensor aggregates, prune.

Each remote job has its own cadence and exponential backoff, so a flaky cellular link
never hammers the APIs and a dead source never delays the others.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .service import NETWORK_ERRORS, SAVER_JOB_FACTOR, NoPosition, WeatherService

log = logging.getLogger(__name__)

TICK_S = 15
BACKOFF_BASE_S = 300
BACKOFF_MAX_S = 3600
NOT_APPLICABLE_RETRY_S = 3 * 3600


@dataclass
class Job:
    name: str
    interval_s: float
    run: Callable[[], Awaitable[None]]
    status_key: str | None = None
    next_at: float = 0.0
    failures: int = 0
    last_error: str | None = field(default=None)


class Scheduler:
    def __init__(self, service: WeatherService):
        self.s = service
        self.jobs = [
            Job("alerts", 600, service.refresh_alerts, "nws_alerts"),
            Job("buoys", 1800, service.refresh_buoys, "ndbc"),
            Job("marine_text", 3600, service.refresh_marine_text, "nws_marine_text"),
            Job("tides", 6 * 3600, service.refresh_tides, "coops"),
        ]
        self.forecast_next_attempt = 0.0
        self.forecast_failures = 0
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._last_tile_prune = 0.0

    def _backoff(self, failures: int) -> float:
        return min(BACKOFF_BASE_S * 2 ** max(failures - 1, 0), BACKOFF_MAX_S)

    async def run_forecast_if_due(self, now: float) -> None:
        if now < self.forecast_next_attempt:
            return
        due, why = self.s.forecast_due()
        if not due:
            return
        log.info("refreshing forecast: %s", why)
        try:
            await self.s.refresh_forecast()
            self.forecast_failures = 0
            self.forecast_next_attempt = now + 60
        except NoPosition:
            self.forecast_next_attempt = now + 300
        except NETWORK_ERRORS as exc:
            self.forecast_failures += 1
            wait = self._backoff(self.forecast_failures)
            self.forecast_next_attempt = now + wait
            log.warning("forecast refresh failed (%s); retry in %.0f min", exc, wait / 60)

    async def run_job(self, job: Job, now: float) -> None:
        if now < job.next_at:
            return
        try:
            await job.run()
        except NoPosition:
            job.next_at = now + 300
            return
        except Exception as exc:  # jobs handle their own network errors; this is a bug guard
            log.exception("job %s crashed", job.name)
            job.failures += 1
            job.last_error = str(exc)
            job.next_at = now + self._backoff(job.failures)
            return
        st = self.s.status.get(job.status_key or "", {})
        if st.get("applicable") is False:
            job.next_at = now + NOT_APPLICABLE_RETRY_S
        elif st.get("error"):
            job.failures += 1
            job.next_at = now + self._backoff(job.failures)
        else:
            job.failures = 0
            factor = SAVER_JOB_FACTOR.get(job.name, 1.0) if self.s.bandwidth().saver else 1.0
            job.next_at = now + job.interval_s * factor

    def flush_sensors(self, now: float) -> None:
        rows = self.s.hub.flush_minute()
        if rows:
            self.s.db.add_observations(rows)
        pos = self.s.hub.position_to_save()
        if pos:
            self.s.db.add_position(pos["ts"], pos["lat"], pos["lon"], "gps")

    def prune(self, now: float) -> None:
        self.s.db.prune_observations(now - self.s.settings.observation_retention_days * 86400)
        self.s.db.prune_runs(now=now)

    async def tick(self) -> None:
        now = self.s.clock()
        if now - self._last_flush >= 60:
            self.flush_sensors(now)
            self._last_flush = now
        if now - self._last_prune >= 86400:
            self.prune(now)
            self._last_prune = now
        if now - self._last_tile_prune >= 1800:
            self._last_tile_prune = now
            self.s.imagery.expire_missing(now)
            try:
                await asyncio.to_thread(self.s.imagery.prune, now)   # a directory walk: off the event loop
            except Exception:                                        # never let it block the jobs below
                log.exception("pruning the image cache failed")
        await self.run_forecast_if_due(now)
        for job in self.jobs:
            await self.run_job(job, now)

    async def run(self) -> None:
        self._last_flush = self.s.clock()
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_S)
