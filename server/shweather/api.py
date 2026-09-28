"""REST API. All handlers read local state, so they work offline.

Handlers are ``async`` on purpose: they run on the event loop thread, the same thread
that feeds the SensorHub, so no locking is needed around live instrument values.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field

from .config import BandwidthConfig, BoatProfile
from .imagery import MAX_ZOOM, ImageryError, image_type
from .service import NoPosition, WeatherService

router = APIRouter(prefix="/api")

DEFAULT_METRICS = "pressure_hpa,tws_kn,tws_max_kn,twd_deg"


def svc(request: Request) -> WeatherService:
    return request.app.state.service


def require_write(request: Request) -> None:
    """Guard for endpoints that change state.

    - A custom header is required, which a browser will not send cross-origin without a
      CORS preflight that this server never approves: another web page open on a crew
      member's phone cannot silently change settings or trigger downloads (CSRF).
    - If ``api_token`` is configured, the token must match as well.
    """
    if not request.headers.get("x-shw-client"):
        raise HTTPException(403, "Missing X-SHW-Client header")
    token = request.app.state.service.settings.api_token
    if token:
        given = request.headers.get("x-shw-token") or ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            given = given or auth[7:]
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "This server requires an access token (Settings > Access token)")


WRITE = [Depends(require_write)]


def may_download(request: Request) -> bool:
    """Whether this request may make the server download something (as the app's requests may).

    Reading what is already cached stays open to everyone, like the rest of the API.
    """
    try:
        require_write(request)
        return True
    except HTTPException:
        return False


class PositionIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    source: str = Field("phone", max_length=32)


@router.get("/status", summary="Service health, position source, per-source status")
async def status(s: WeatherService = Depends(svc)):
    return s.status_view()


@router.get("/now", summary="Cockpit snapshot: instruments, forecast now, tendency, alerts, assessment")
async def now(s: WeatherService = Depends(svc)):
    try:
        return s.now()
    except NoPosition as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/forecast", summary="Hourly forecast interpolated to a position, with local correction")
async def forecast(
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
    hours: int = Query(72, ge=1, le=384),
    past_hours: int = Query(6, ge=0, le=24),
    s: WeatherService = Depends(svc),
):
    try:
        view = s.forecast_view(lat, lon, hours, past_hours)
    except NoPosition as exc:
        raise HTTPException(409, str(exc)) from exc
    if not view:
        raise HTTPException(404, "No forecast stored for this area yet. It will download automatically "
                                 "when the Pi is online, or POST /api/refresh.")
    return view


@router.get("/observations", summary="Onboard 1-minute averaged time series")
async def observations(metrics: str = DEFAULT_METRICS, hours: float = Query(24, gt=0, le=24 * 60),
                       s: WeatherService = Depends(svc)):
    names = [m.strip() for m in metrics.split(",") if m.strip()][:12]
    return {"hours": hours, "series": s.observations(names, hours)}


@router.get("/alerts", summary="Active NWS alerts for the position (cached)")
async def alerts(s: WeatherService = Depends(svc)):
    return s.cached("alerts")


@router.get("/marine-text", summary="NWS marine zone text forecast (cached)")
async def marine_text(s: WeatherService = Depends(svc)):
    return s.cached("marine_text")


@router.get("/buoys", summary="Nearest NDBC buoy / C-MAN observations (cached)")
async def buoys(s: WeatherService = Depends(svc)):
    return s.cached("buoys")


@router.get("/tides", summary="Nearest NOAA tide and tidal-current predictions (cached)")
async def tides(s: WeatherService = Depends(svc)):
    return s.cached("tides")


@router.post("/position", dependencies=WRITE, summary="Set position, e.g. from a phone's GPS when the Pi has none")
async def set_position(body: PositionIn, s: WeatherService = Depends(svc)):
    return s.set_position(body.lat, body.lon, body.source)


@router.post("/refresh", dependencies=WRITE,
             summary="Fetch now. radius_nm downloads a wider passage grid for offline use")
async def refresh(radius_nm: float | None = Query(None, gt=0, le=600), s: WeatherService = Depends(svc)):
    try:
        return await s.refresh_all(radius_nm)
    except NoPosition as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/map", summary="Model wind, cloud and rain on the forecast grid, for the Radar tab")
async def map_fields(past_hours: int = Query(3, ge=0, le=24), hours: int = Query(48, ge=1, le=240),
                     s: WeatherService = Depends(svc)):
    try:
        view = s.map_fields(past_hours, hours)
    except NoPosition as exc:
        raise HTTPException(409, str(exc)) from exc
    if not view:
        raise HTTPException(404, "No forecast stored for this area yet.")
    return view


@router.get("/imagery", summary="Radar and satellite frames available for the Radar tab")
async def imagery(request: Request, s: WeatherService = Depends(svc)):
    try:
        return await s.imagery.view(may_fetch=may_download(request))
    except NoPosition as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/tiles/{layer}/{frame}/{z}/{x}/{y}.png", summary="A radar, satellite or map tile (cached by the server)",
            response_class=Response)
async def tile(request: Request, layer: str, frame: int = Path(ge=0), z: int = Path(ge=0, le=MAX_ZOOM),
               x: int = Path(ge=0), y: int = Path(ge=0), s: WeatherService = Depends(svc)):
    try:
        data = await s.imagery.tile(layer, frame, z, x, y, may_fetch=may_download(request))
    except ImageryError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except NoPosition as exc:
        raise HTTPException(409, str(exc)) from exc
    # A frame's image never changes; the optional base map changes rarely.
    age = 7 * 86400 if layer == "basemap" else 86400
    return Response(data, media_type=image_type(data) or "application/octet-stream",
                    headers={"Cache-Control": f"public, max-age={age}"})


@router.get("/boat", response_model=BoatProfile, summary="Boat profile used for reef / no-go assessment")
async def get_boat(s: WeatherService = Depends(svc)):
    return s.boat()


@router.put("/boat", response_model=BoatProfile, dependencies=WRITE)
async def put_boat(profile: BoatProfile, s: WeatherService = Depends(svc)):
    return s.set_boat(profile)


@router.get("/bandwidth", summary="Bandwidth limits and data usage (today, this month, last 31 days)")
async def get_bandwidth(s: WeatherService = Depends(svc)):
    return s.bandwidth_view()


@router.put("/bandwidth", response_model=BandwidthConfig, dependencies=WRITE,
            summary="Change bandwidth limits at runtime (overrides config.yaml, persisted)")
async def put_bandwidth(cfg: BandwidthConfig, s: WeatherService = Depends(svc)):
    return s.set_bandwidth(cfg)
