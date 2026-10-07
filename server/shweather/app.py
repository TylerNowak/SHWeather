"""FastAPI application factory and process lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import logging
import mimetypes
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .api import router
from .config import Settings, load_settings
from .scheduler import Scheduler
from .sensors.bme280 import run_bme280
from .sensors.readers import run_nmea_source
from .sensors.signalk import run_signalk
from .service import WeatherService
from .tls import HttpsListener

log = logging.getLogger(__name__)

# Python's mimetypes reads the Windows registry, where .js is often "text/plain"; browsers
# then refuse the app's ES modules and the page stays blank. The web app's own file types
# are therefore pinned when serving, whatever the registry says.
WEB_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}
for _ext, _type in WEB_TYPES.items():
    mimetypes.add_type(_type.split(";")[0], _ext)

DESCRIPTION = """Self-hosted, offline-first marine weather for a Raspberry Pi or Windows PC aboard.

Every endpoint answers from local state, so it keeps working at sea without internet.
**Not for navigation**: supplements official forecasts and your own judgement."""


def build_service(settings: Settings) -> WeatherService:
    if settings.demo:
        from .demo import build_demo_service
        return build_demo_service(settings)
    return WeatherService(settings)


def background_tasks(service: WeatherService) -> list[asyncio.Task]:
    st = service.settings
    tasks = [asyncio.create_task(run_nmea_source(src, service.hub), name=f"nmea-{i}")
             for i, src in enumerate(st.sensors.nmea)]
    if st.sensors.signalk:
        tasks.append(asyncio.create_task(run_signalk(st.sensors.signalk, service.hub), name="signalk"))
    if st.sensors.bme280:
        tasks.append(asyncio.create_task(run_bme280(st.sensors.bme280, service.hub), name="bme280"))
    if st.demo:
        from .demo import run_demo_instruments
        tasks.append(asyncio.create_task(run_demo_instruments(service), name="demo-instruments"))
    tasks.append(asyncio.create_task(Scheduler(service).run(), name="scheduler"))
    return tasks


class TextGzip:
    """gzip for JSON, HTML, JS and CSS; images (radar tiles) pass straight through.

    Starlette's GZipMiddleware would also recompress PNG tiles, spending a Pi's CPU for
    nothing. Responses here are small and not streamed, so buffering them is fine.
    """

    TYPES = ("application/json", "text/", "application/javascript", "image/svg+xml", "application/manifest+json")

    def __init__(self, app: ASGIApp, minimum_size: int = 1024, level: int = 6):
        self.app, self.minimum_size, self.level = app, minimum_size, level

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "HEAD" or \
                "gzip" not in Headers(scope=scope).get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return
        start: Message | None = None
        compress = False
        chunks: list[bytes] = []

        async def wrapped(message: Message) -> None:
            nonlocal start, compress
            if message["type"] == "http.response.start":
                headers = Headers(raw=message["headers"])
                compress = (message["status"] == 200 and "content-encoding" not in headers
                            and headers.get("content-type", "").startswith(self.TYPES))
                if compress:
                    start = message
                else:
                    await send(message)
                return
            if message["type"] != "http.response.body" or not compress:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = MutableHeaders(raw=start["headers"])
            if len(body) >= self.minimum_size:
                body = gzip.compress(body, self.level)
                headers["Content-Encoding"] = "gzip"
                headers.add_vary_header("Accept-Encoding")
            headers["Content-Length"] = str(len(body))
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)


# Revalidated on every load (a cheap 304 when unchanged), so phones pick up an upgraded app
# at once instead of mixing a new page with old cached scripts.
REVALIDATE = {".html", ".js", ".mjs", ".css", ".json", ".webmanifest"}


class PWAStaticFiles(StaticFiles):
    """Static files with the right types; the app's code is always revalidated."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        ext = os.path.splitext(path)[1].lower() or ".html"  # "" -> index.html
        if resp.status_code == 200 and ext in WEB_TYPES:
            resp.headers["content-type"] = WEB_TYPES[ext]
        if ext in REVALIDATE:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(settings: Settings | None = None, service: WeatherService | None = None,
               start_background: bool = True, serve_https: bool = False) -> FastAPI:
    """The web app and API. ``serve_https`` also serves it over HTTPS on ``https_port``
    (``shweather serve`` does; tests don't)."""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or build_service(settings)
        app.state.service = svc
        tasks = background_tasks(svc) if start_background else []
        https = HttpsListener(app, settings, svc) if serve_https and settings.https_port else None
        if https:
            https.start()
        log.info("SHWeather %s started (demo=%s)", __version__, settings.demo)
        try:
            yield
        finally:
            if https:
                await https.stop()
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            # Persist whatever the sensors collected since the last minute flush.
            rows = svc.hub.flush_minute()
            if rows:
                svc.db.add_observations(rows)
            svc.save_positions()
            await svc.aclose()

    app = FastAPI(title="SHWeather", version=__version__, description=DESCRIPTION, lifespan=lifespan)
    app.add_middleware(TextGzip)
    app.include_router(router)
    web_root = settings.resolved_web_root()
    if web_root.is_dir():
        app.mount("/", PWAStaticFiles(directory=web_root, html=True), name="web")
    else:
        log.warning("web root %s not found; serving API only", web_root)
    return app
