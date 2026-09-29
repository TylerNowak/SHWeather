"""Live weather imagery for the Radar tab: radar and satellite tiles.

The server downloads and caches the tiles, so phones never need the internet themselves,
and several phones looking at the same map cost the boat's link one download.

Nothing is downloaded until someone opens the Radar tab: frame lists are refreshed when
the app asks for them, and tiles when the app draws them. Every download goes through the
shared HTTP client, so the speed cap and data caps apply, and imagery never touches the
data reserved for safety alerts. In data-saver mode only the latest frame is offered.

Sources (all configurable under ``imagery:`` in config.yaml):

- **radar: rainviewer** (default, worldwide): the RainViewer Weather Maps API, past two
  hours at 10-minute steps, zoom <= 7, free for personal use with attribution. Any
  RainViewer-compatible server works too, e.g. a self-hosted LibreWXR, which can also
  supply satellite frames and radar nowcasts.
- **radar: iem** (US and Great Lakes): the NEXRAD composite tiles of the Iowa Environmental
  Mesonet (NOAA data), 5-minute updates, more detail than RainViewer's free tier.
- **satellite: gibs**: NASA GIBS infrared (ABI/AHI band 13) from the geostationary
  satellite nearest the boat (GOES-East, GOES-West or Himawari), every 10 minutes; works
  day and night. No GIBS geostationary coverage over Europe, Africa and the Indian Ocean.
- **basemap_url**: optional online map tiles under the weather. The app draws its own
  offline land and lakes map, so this is off by default.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import hashlib
import logging
import math
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import ImageryConfig, Settings
from .db import Database
from .geo import distance_nm, normalize_lon
from .net import BudgetExceeded, Http

log = logging.getLogger(__name__)

LAYERS = ("radar", "satellite", "basemap")
FRAME_STEP_S = 600                  # loops use 10-minute steps
FRAME_LIST_TTL_S = {"radar": 300, "satellite": 600, "basemap": 86400}
MISSING_TTL_S = 600                 # a tile the source doesn't have yet is retried after this
KEEP_FRAMES_S = 6 * 3600            # radar/satellite tiles are deleted after this
MAX_TILE_DISTANCE_NM = 600          # tiles further than this from the boat are refused
EMPTY_TILE_BYTES = 1500             # a smaller PNG is a blank tile (used when probing GIBS)
PROBE_STEPS = 12                    # look back up to 2 h for the latest satellite image
MAX_ZOOM = 18
PARALLEL_DOWNLOADS = 3              # per server, whatever the number of phones
REQUESTS_PER_MINUTE = 80            # per image host (RainViewer allows 100 per IP)

# Geostationary satellites with 10-minute infrared imagery in NASA GIBS.
GEO_SATELLITES = (
    ("GOES-East_ABI_Band13_Clean_Infrared", -75.2, "NOAA GOES-East"),
    ("GOES-West_ABI_Band13_Clean_Infrared", -137.2, "NOAA GOES-West"),
    ("Himawari_AHI_Band13_Clean_Infrared", 140.7, "JMA Himawari"),
)
MAX_VIEW_ANGLE_DEG = 70.0           # beyond this from the sub-satellite point the view is too oblique
GIBS_LEVELS = (6, 7, 5, 8)          # tile matrix sets to try, most likely first


class ImageryError(Exception):
    """A tile or frame list that can't be delivered; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Frame:
    time: int
    url: str                        # upstream template with {z}, {x}, {y}
    forecast: bool = False


@dataclass
class FrameList:
    frames: list[Frame]
    max_zoom: int
    source: str
    attribution: str
    link: str | None = None
    fetched_at: float = 0.0
    note: str | None = None


# ---------------------------------------------------------------- tile maths (Web Mercator)

def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a slippy-map tile."""
    n = 2 ** z

    def lat(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return lat(y + 1), x / n * 360 - 180, lat(y), (x + 1) / n * 360 - 180


def tile_at(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    lat = max(-85.0511, min(85.0511, lat))
    x = int((normalize_lon(lon) + 180) / 360 * n) % n
    r = math.radians(lat)
    y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return x, min(max(y, 0), n - 1)


def distance_to_tile_nm(lat: float, lon: float, z: int, x: int, y: int) -> float:
    """Distance from a position to the nearest point of a tile (0 inside it)."""
    s, w, n, e = tile_bounds(z, x, y)
    clat = min(max(lat, s), n)
    lon_rel = normalize_lon(lon - (w + e) / 2)          # handles tiles across the antimeridian
    half = (e - w) / 2
    clon = (w + e) / 2 + min(max(lon_rel, -half), half)
    return distance_nm(lat, lon, clat, clon)


def pick_satellite(lon: float) -> tuple[str, float, str] | None:
    best = min(GEO_SATELLITES, key=lambda s: abs(normalize_lon(lon - s[1])))
    return best if abs(normalize_lon(lon - best[1])) <= MAX_VIEW_ANGLE_DEG else None


def image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def utc(t: float, fmt: str) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime(fmt)


def fill(template: str, z: int, x: int, y: int) -> str:
    """A tile URL from a {z}/{x}/{y} template; {s} (server letter) and {r} (retina) are common too."""
    return (template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
            .replace("{s}", "a").replace("{r}", ""))


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:8]


# ---------------------------------------------------------------- the service

class Imagery:
    def __init__(self, settings: Settings, http: Http, db: Database, *, position: Callable[[], dict],
                 saver: Callable[[], bool], clock: Callable[[], float] = time.time):
        self.settings = settings
        self.http = http
        self.db = db
        self.position = position
        self.saver = saver
        self.clock = clock
        self.root = Path(settings.data_dir) / "tiles"
        self._lists: dict[str, FrameList] = {}
        self._errors: dict[str, tuple[float, str]] = {}
        self._missing: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._list_locks = {layer: asyncio.Lock() for layer in LAYERS}
        self._downloads = asyncio.Semaphore(PARALLEL_DOWNLOADS)
        self._recent: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    @property
    def cfg(self) -> ImageryConfig:
        return self.settings.imagery

    def enabled(self, layer: str) -> bool:
        c = self.cfg
        if not c.enabled:
            return False
        return {"radar": c.radar != "none", "satellite": c.satellite != "none",
                "basemap": bool(c.basemap_url)}[layer]

    def source_key(self, layer: str) -> str:
        """Which source a layer's tiles come from, from config and position alone. Part of the
        cache path, so switching source (or crossing from GOES-East to GOES-West) never serves
        the other source's pictures."""
        c = self.cfg
        if layer == "radar":
            return f"{c.radar}-{short_hash(c.iem_url if c.radar == 'iem' else c.rainviewer_url)}"
        if layer == "satellite":
            if c.satellite == "rainviewer":
                return f"rv-{short_hash(c.rainviewer_url)}"
            sat = pick_satellite(self.position()["lon"])
            return f"gibs-{short_hash(c.gibs_url)}-{sat[0].split('_')[0] if sat else 'none'}"
        return f"map-{short_hash(c.basemap_url or '')}"

    # ------------------------------------------------------------ frame lists

    async def frame_list(self, layer: str, refresh: bool = True) -> FrameList:
        if not self.enabled(layer):
            raise ImageryError(404, f"{layer} imagery is turned off in config.yaml")
        async with self._list_locks[layer]:
            cur = self._lists.get(layer)
            now = self.clock()
            if cur and (not refresh or now - cur.fetched_at < FRAME_LIST_TTL_S[layer]):
                return cur
            if not refresh:
                raise ImageryError(404, f"no {layer} frames yet")
            try:
                fl = await getattr(self, f"_list_{layer}")(now)
            except ImageryError as exc:
                self._errors[layer] = (now, str(exc))
                if cur:
                    return cur          # keep showing the frames we have
                raise
            except BudgetExceeded as exc:
                self._errors[layer] = (now, str(exc))
                if cur:
                    return cur
                raise ImageryError(503, f"Live {layer} paused: {exc}") from exc
            except httpx.HTTPError as exc:
                self._errors[layer] = (now, f"no connection ({exc.__class__.__name__})")
                if cur:
                    return cur
                raise ImageryError(503, f"Live {layer} needs the internet: no connection") from exc
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                # An upstream frame list in an unexpected shape.
                log.warning("unexpected %s frame list: %r", layer, exc)
                self._errors[layer] = (now, "the source sent an unexpected frame list")
                if cur:
                    return cur
                raise ImageryError(502, f"the {layer} source sent an unexpected frame list") from exc
            fl.fetched_at = now
            self._errors.pop(layer, None)
            self._lists[layer] = fl
            return fl

    def _trim(self, frames: list[Frame], step_s: int = FRAME_STEP_S) -> list[Frame]:
        """The loop: frames on fixed ``step_s`` marks within ``loop_minutes`` of the newest;
        only the newest in saver mode.

        Fixed marks matter: a loop counted back from the newest frame would shift with every
        new frame, and the whole loop would be downloaded again on each refresh. With 20-minute
        satellite steps, a new satellite picture is downloaded every 20 minutes, not every 10.
        """
        past = sorted((f for f in frames if not f.forecast), key=lambda f: f.time)
        ahead = sorted((f for f in frames if f.forecast), key=lambda f: f.time)
        if not past:
            return []
        newest = past[-1]
        if self.saver():
            return [newest]
        marks = [f for f in past if f.time % step_s == 0]
        # Count the loop back from the newest mark, so it has the same length whether or not
        # the newest frame falls on one.
        start = (marks[-1] if marks else newest).time - self.cfg.loop_minutes * 60
        on_marks = [f for f in marks if f.time >= start]
        if not on_marks:                       # a source with frames off the marks: space them out
            last = None
            for f in reversed(past):
                if f.time < start:
                    break
                if last is None or last - f.time >= step_s - 60:
                    on_marks.insert(0, f)
                    last = f.time
        return on_marks + ahead

    async def _list_radar(self, now: float) -> FrameList:
        c = self.cfg
        if c.radar == "iem":
            step = 300
            latest = int((now - 600) // step * step)   # composites appear a few minutes late
            base = c.iem_url.rstrip("/")
            frames = [Frame(t, f"{base}/cache/tile.py/1.0.0/ridge::USCOMP-N0Q-{utc(t, '%Y%m%d%H%M')}/{{z}}/{{x}}/{{y}}.png")
                      for t in range(latest - c.loop_minutes * 60 - FRAME_STEP_S, latest + 1, step)]
            return FrameList(self._trim(frames), 8, "iem", "Radar: NOAA NEXRAD via Iowa Environmental Mesonet",
                             "https://mesonet.agron.iastate.edu/", note="US and nearby waters only")
        data = await self._rainviewer_maps()
        host = (data.get("host") or c.rainviewer_url).rstrip("/")
        radar = data.get("radar") or {}
        frames = [Frame(int(f["time"]), f"{host}{f['path']}/256/{{z}}/{{x}}/{{y}}/2/1_1.png")
                  for f in radar.get("past") or [] if f.get("path")]
        frames += [Frame(int(f["time"]), f"{host}{f['path']}/256/{{z}}/{{x}}/{{y}}/2/1_1.png", forecast=True)
                   for f in radar.get("nowcast") or [] if f.get("path")]
        if not frames:
            raise ImageryError(502, "the radar source listed no frames")
        return FrameList(self._trim(frames), c.rainviewer_max_zoom, "rainviewer", "Weather data by RainViewer",
                         "https://www.rainviewer.com/")

    async def _rainviewer_maps(self) -> dict:
        url = self.cfg.rainviewer_url.rstrip("/") + "/public/weather-maps.json"
        resp = await self.http.get(url)
        if resp.status_code != 200:
            raise ImageryError(502, f"radar source HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ImageryError(502, "radar source sent an unreadable frame list") from exc
        if not isinstance(data, dict):
            raise ImageryError(502, "radar source sent an unexpected frame list")
        return data

    async def _list_satellite(self, now: float) -> FrameList:
        c = self.cfg
        if c.satellite == "rainviewer":
            # RainViewer-compatible servers (e.g. a self-hosted LibreWXR) may list infrared frames.
            data = await self._rainviewer_maps()
            host = (data.get("host") or c.rainviewer_url).rstrip("/")
            frames = [Frame(int(f["time"]), f"{host}{f['path']}/256/{{z}}/{{x}}/{{y}}/0/0_0.png")
                      for f in (data.get("satellite") or {}).get("infrared") or [] if f.get("path")]
            if not frames:
                raise ImageryError(404, "the RainViewer-compatible server lists no satellite frames")
            return FrameList(self._trim(frames, c.satellite_step_minutes * 60), c.rainviewer_max_zoom, "rainviewer",
                             "Satellite: RainViewer-compatible server")
        pos = self.position()
        sat = pick_satellite(pos["lon"])
        if not sat:
            raise ImageryError(404, "NASA GIBS has no geostationary imagery for this longitude "
                                    "(Europe, Africa and the Indian Ocean are not covered)")
        layer, _, name = sat
        level = await self._gibs_level(layer)
        base = f"{c.gibs_url.rstrip('/')}/wmts/epsg3857/best/{layer}/default"
        step = c.satellite_step_minutes * 60
        latest = await self._gibs_latest(base, level, pos, now, step)
        frames = [Frame(t, f"{base}/{utc(t, '%Y-%m-%dT%H:%M:%SZ')}/GoogleMapsCompatible_Level{level}/{{z}}/{{y}}/{{x}}.png")
                  for t in range(latest - c.loop_minutes * 60, latest + 1, step)]
        return FrameList(self._trim(frames, c.satellite_step_minutes * 60), level, "gibs",
                         f"Satellite: {name} infrared via NASA GIBS",
                         "https://earthdata.nasa.gov/gibs")

    async def _gibs_level(self, layer: str) -> int:
        """Which GoogleMapsCompatible_LevelN tile matrix set GIBS offers this layer in (checked once)."""
        key = f"gibs_level:{layer}"
        hit = self.db.kv_get(key, max_age_s=30 * 86400)
        if hit:
            return int(hit[0])
        base = f"{self.cfg.gibs_url.rstrip('/')}/wmts/epsg3857/best/{layer}/default/default"
        for level in GIBS_LEVELS:
            resp = await self.http.get(f"{base}/GoogleMapsCompatible_Level{level}/0/0/0.png")
            if resp.status_code == 200 and image_type(resp.content):
                self.db.kv_set(key, level)
                return level
        raise ImageryError(502, "NASA GIBS did not offer the satellite layer in Web Mercator")

    async def _gibs_latest(self, base: str, level: int, pos: dict, now: float, step: int = FRAME_STEP_S) -> int:
        """Newest loop mark (every ``step`` seconds) GIBS has an image for, found by trying the
        tile under the boat. The tile found is part of the loop, so the probe costs nothing extra."""
        key = f"gibs_latest:{base}"
        hit = self.db.kv_get(key, max_age_s=FRAME_LIST_TTL_S["satellite"])
        if hit:
            return int(hit[0])
        x, y = tile_at(pos["lat"], pos["lon"], level)
        slot = int(now // step * step)
        for k in range(1, max(2, PROBE_STEPS * FRAME_STEP_S // step) + 1):
            t = slot - k * step
            url = f"{base}/{utc(t, '%Y-%m-%dT%H:%M:%SZ')}/GoogleMapsCompatible_Level{level}/{level}/{y}/{x}.png"
            resp = await self.http.get(url)
            if resp.status_code == 200 and image_type(resp.content) and len(resp.content) >= EMPTY_TILE_BYTES:
                try:
                    self._store(self._path("satellite", t, level, x, y), resp.content)
                except OSError as exc:
                    log.warning("could not cache satellite tile: %s", exc)
                self.db.kv_set(key, t)
                return t
        raise ImageryError(404, "no recent satellite image found")

    async def _list_basemap(self, now: float) -> FrameList:
        c = self.cfg
        return FrameList([Frame(0, c.basemap_url)], c.basemap_max_zoom, "basemap", c.basemap_attribution)

    # ------------------------------------------------------------ tiles

    def _path(self, layer: str, frame: int, z: int, x: int, y: int, key: str | None = None) -> Path:
        return self.root / layer / (key or self.source_key(layer)) / str(frame) / str(z) / str(x) / f"{y}.img"

    @staticmethod
    def _store(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    async def tile(self, layer: str, frame: int, z: int, x: int, y: int, *, may_fetch: bool) -> bytes:
        """The tile's bytes, from the cache or downloaded (only when ``may_fetch``)."""
        if layer not in LAYERS:
            raise ImageryError(404, "unknown layer")
        if not 0 <= z <= MAX_ZOOM:              # before 2 ** z: a huge z would stall the server
            raise ImageryError(404, "no such tile")
        n = 2 ** z
        if not (0 <= x < n and 0 <= y < n and frame >= 0):
            raise ImageryError(404, "no such tile")
        path = self._path(layer, frame, z, x, y)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            pass
        if not self.enabled(layer):
            raise ImageryError(404, f"{layer} imagery is turned off in config.yaml")
        if not may_fetch:
            raise ImageryError(404, "not downloaded yet")
        key = f"{layer}/{frame}/{z}/{x}/{y}"
        now = self.clock()
        if self._missing.get(key, 0) > now:
            raise ImageryError(404, "the source has no image here yet")
        fl = await self.frame_list(layer, refresh=False) if layer in self._lists else await self.frame_list(layer)
        if z > fl.max_zoom:
            raise ImageryError(404, f"{layer} goes up to zoom {fl.max_zoom}")
        found = next((f for f in fl.frames if f.time == frame), None)
        if not found:
            raise ImageryError(404, "this frame is no longer offered")
        pos = self.position()
        if z >= 4 and distance_to_tile_nm(pos["lat"], pos["lon"], z, x, y) > MAX_TILE_DISTANCE_NM:
            raise ImageryError(403, f"tiles are only fetched within {MAX_TILE_DISTANCE_NM} nm of the boat")
        task = self._inflight.get(key)
        if task is None:
            # The download runs as its own task: a phone giving up doesn't cancel it for the
            # others waiting on the same tile.
            task = asyncio.create_task(self._fetch_and_store(layer, fill(found.url, z, x, y), key, path))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        return await asyncio.shield(task)

    async def _fetch_and_store(self, layer: str, url: str, key: str, path: Path) -> bytes:
        data = await self._download(layer, url, key)
        try:
            self._store(path, data)
        except OSError as exc:                     # e.g. a full SD card: still show the picture
            log.warning("could not cache %s tile: %s", layer, exc)
        return data

    async def _throttle(self, url: str) -> None:
        """At most REQUESTS_PER_MINUTE to one host (RainViewer blocks clients above 100/min)."""
        q = self._recent[urlsplit(url).netloc]
        while True:
            now = time.monotonic()
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) < REQUESTS_PER_MINUTE:
                q.append(now)
                return
            await asyncio.sleep(60 - (now - q[0]) + 0.05)

    async def _download(self, layer: str, url: str, key: str) -> bytes:
        try:
            async with self._downloads:
                await self._throttle(url)
                resp = await self.http.get(url)
        except BudgetExceeded as exc:
            raise ImageryError(503, f"Live {layer} paused: {exc}") from exc
        except httpx.HTTPError as exc:
            self._errors[layer] = (self.clock(), f"no connection ({exc.__class__.__name__})")
            raise ImageryError(503, f"Live {layer} needs the internet: no connection") from exc
        if resp.status_code in (204, 400, 404):
            self._missing[key] = self.clock() + MISSING_TTL_S
            raise ImageryError(404, "the source has no image here yet")
        if resp.status_code != 200:
            raise ImageryError(502, f"{layer} source HTTP {resp.status_code}")
        if not image_type(resp.content):
            raise ImageryError(502, f"{layer} source did not send an image")
        return resp.content

    # ------------------------------------------------------------ view + housekeeping

    async def view(self, may_fetch: bool) -> dict:
        """Frame lists and state for the app. Frame lists refresh only when ``may_fetch``."""
        budget_note = None
        try:
            self.http.allowance()
        except BudgetExceeded as exc:
            budget_note = str(exc)
        out: dict = {"enabled": self.cfg.enabled, "saver": self.saver(), "paused": budget_note, "layers": {}}
        for layer in LAYERS:
            if not self.enabled(layer):
                continue
            entry: dict = {"frames": [], "error": None}
            try:
                fl = await self.frame_list(layer, refresh=may_fetch and budget_note is None)
                # Pictures older than the cache keeps them are gone (and would be mislabelled).
                oldest = self.clock() - KEEP_FRAMES_S
                entry.update({
                    "frames": [{"time": f.time, "forecast": f.forecast} for f in fl.frames
                               if layer == "basemap" or f.time >= oldest],
                    "max_zoom": fl.max_zoom, "source": fl.source, "attribution": fl.attribution,
                    "link": fl.link, "note": fl.note, "age_s": round(self.clock() - fl.fetched_at),
                    "key": self.source_key(layer),
                })
            except ImageryError as exc:
                entry["error"] = str(exc)
            except Exception:                  # a bug must not take the whole tab down
                log.exception("imagery view for %s failed", layer)
                entry["error"] = "unexpected error (see the server log)"
            err = self._errors.get(layer)
            if err and not entry["error"]:
                entry["warning"] = err[1]
            out["layers"][layer] = entry
        return out

    def expire_missing(self, now: float | None = None) -> None:
        """Forget "the source has no image" answers that have expired (event loop only)."""
        now = now or self.clock()
        self._missing = {k: v for k, v in self._missing.items() if v > now}

    def prune(self, now: float | None = None) -> None:
        """Delete old radar/satellite frames, then the oldest tiles while over the size cap.

        Only touches files, so it can run in a worker thread. Never raises: a locked file
        (Windows, antivirus) is simply left for the next round.
        """
        now = now or self.clock()
        try:
            for layer in ("radar", "satellite"):
                for frame_dir in self.root.glob(f"{layer}/*/*"):
                    if frame_dir.name.isdigit() and int(frame_dir.name) < now - KEEP_FRAMES_S:
                        shutil.rmtree(frame_dir, ignore_errors=True)
            cap = self.cfg.cache_mb * 1e6
            files = []
            for p in self.root.rglob("*.img") if self.root.is_dir() else []:
                try:
                    st = p.stat()
                    files.append((st.st_mtime, st.st_size, p))
                except OSError:
                    continue
            total = sum(sz for _, sz, _ in files)
            for _, sz, p in sorted(files, key=lambda f: f[0]):
                if total <= cap:
                    break
                try:
                    p.unlink(missing_ok=True)
                    total -= sz
                except OSError:
                    continue
        except OSError as exc:
            log.warning("pruning the image cache failed: %s", exc)
