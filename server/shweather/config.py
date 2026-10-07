r"""Configuration: a YAML file mapped onto pydantic models.

Lookup order for the config file: explicit path > $SHWEATHER_CONFIG > ./config.yaml >
the system location (/etc/shweather/config.yaml on Linux,
%PROGRAMDATA%\SHWeatherService\config.yaml on Windows). Every field has a default, so an
empty file is valid.

Paths in the file may use ~ and environment variables ($HOME, %PROGRAMDATA%), and
relative paths are resolved against the directory containing the config file, so a
service started from any working directory (e.g. C:\Windows\System32) finds its data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Position(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class GridConfig(BaseModel):
    steps: int = Field(2, ge=1, le=6, description="Grid half-width in points; 2 -> 5x5 grid")
    spacing_deg: float = Field(0.25, gt=0.01, le=2.0)


class ForecastConfig(BaseModel):
    refresh_minutes: int = Field(60, ge=10)
    forecast_days: int = Field(5, ge=1, le=16)
    models: str = Field("best_match", description="Open-Meteo model id(s), e.g. best_match, ecmwf_ifs025")
    grid: GridConfig = GridConfig()
    refetch_distance_nm: float = 10.0
    passage_max_points: int = 400


class BandwidthConfig(BaseModel):
    """Limits on how much, and how fast, the Pi pulls weather data.

    Every field can also be changed at runtime from the app (PUT /api/bandwidth); runtime
    changes are stored in the database and override this file.
    """

    max_kbps: float | None = Field(
        None, gt=0, le=1_000_000,
        description="Download speed cap in kilobits per second across all fetches; empty = unlimited")
    daily_mb: float | None = Field(None, gt=0, description="Data cap per UTC day in MB; empty = unlimited")
    monthly_mb: float | None = Field(None, gt=0, description="Data cap per calendar month (UTC) in MB")
    alert_reserve_pct: float = Field(
        10.0, ge=0, le=50,
        description="Last share of each cap reserved for safety alerts (NWS warnings are tiny)")
    saver: bool = Field(
        False, description="Data saver: 3x3 grid, 3-day forecast, fetches spaced 2-4x further apart")


class SourcesConfig(BaseModel):
    open_meteo: bool = True
    open_meteo_url: str = "https://api.open-meteo.com"
    open_meteo_marine_url: str = "https://marine-api.open-meteo.com"
    open_meteo_apikey: str | None = None
    nws: bool = True
    nws_url: str = "https://api.weather.gov"
    ndbc: bool = True
    ndbc_url: str = "https://www.ndbc.noaa.gov"
    ndbc_radius_nm: float = 60.0
    coops: bool = True
    coops_url: str = "https://api.tidesandcurrents.noaa.gov"
    coops_radius_nm: float = 25.0
    marine_zone: str | None = Field(None, description="Override NWS marine zone, e.g. LMZ740")
    contact: str = Field("admin@example.com",
                         description="Contact put in the User-Agent; api.weather.gov requires one")
    timeout_s: float = 20.0


def default_serial_device(platform: str = sys.platform) -> str:
    return "COM3" if platform == "win32" else "/dev/ttyUSB0"


class NmeaSource(BaseModel):
    type: Literal["tcp", "udp", "serial", "file"]
    host: str | None = Field(None, description="tcp: server to connect to (default 127.0.0.1); "
                                               "udp: local address to listen on (default all interfaces)")
    port: int = 10110
    device: str = Field(default_factory=default_serial_device,
                        description="Serial port: /dev/ttyUSB0, /dev/ttyACM0 (Linux) or COM3 (Windows)")
    baudrate: int = 4800
    path: str | None = None
    loop: bool = False
    rate_hz: float = 5.0  # replay speed for type=file


class SignalKConfig(BaseModel):
    url: str = "http://localhost:3000"
    token: str | None = None
    poll_seconds: float = 2.0


class Bme280Config(BaseModel):
    i2c_bus: int = 1
    address: int = 0x76
    interval_seconds: float = 30.0


class SensorsConfig(BaseModel):
    nmea: list[NmeaSource] = []
    signalk: SignalKConfig | None = None
    bme280: Bme280Config | None = None
    pressure_altitude_m: float = Field(
        0.0, description="Height of the barometer above MSL (lake elevation + mounting height)")
    pressure_offset_hpa: float = Field(0.0, description="Calibration offset added to barometer readings")


class BoatProfile(BaseModel):
    name: str = "My boat"
    reef1_kn: float = 15.0
    reef2_kn: float = 20.0
    max_wind_kn: float = 25.0
    max_gust_kn: float = 32.0
    max_wave_m: float = 2.0
    min_visibility_nm: float = 1.0
    min_wave_period_ratio: float = Field(
        0.0, description="Warn when period (s) < ratio * height (m); short steep seas. 0 disables")


class DisplayConfig(BaseModel):
    """Default display units for devices that haven't chosen their own."""

    units: Literal["auto", "metric", "imperial"] = Field(
        "auto", description="auto: each phone follows its region (imperial in the US)")
    nautical: bool = Field(True, description="Knots and nautical miles for wind, current and distance")


class ImageryConfig(BaseModel):
    """Live radar and satellite images for the Radar tab (see shweather/imagery.py).

    Downloads happen only while someone has the Radar tab open, count against the
    bandwidth caps, and never use the data reserved for safety alerts. The tab's forecast
    layers (wind, clouds, rain) and its offline land map work without any of this.
    """

    enabled: bool = Field(True, description="Download live radar and satellite images for the Radar tab")
    radar: Literal["rainviewer", "iem", "none"] = Field(
        "rainviewer", description="rainviewer: worldwide; iem: NOAA NEXRAD composite, US and Great Lakes only")
    satellite: Literal["gibs", "rainviewer", "none"] = Field(
        "gibs", description="gibs: NASA GIBS infrared from GOES-East/West or Himawari; "
                            "rainviewer: frames from a RainViewer-compatible server such as LibreWXR")
    rainviewer_url: str = Field("https://api.rainviewer.com",
                                description="RainViewer API, or your own RainViewer-compatible server (LibreWXR)")
    rainviewer_max_zoom: int = Field(7, ge=1, le=14, description="RainViewer's free tiles stop at zoom 7")
    iem_url: str = "https://mesonet.agron.iastate.edu"
    gibs_url: str = "https://gibs.earthdata.nasa.gov"
    basemap_url: str | None = Field(
        None, description="Optional online map under the weather, {z}/{x}/{y} tile URL; empty = the app's "
                          "own offline land map only")
    basemap_max_zoom: int = Field(12, ge=1, le=19)
    basemap_attribution: str = ""
    loop_minutes: int = Field(60, ge=0, le=180, description="Length of the radar/satellite loop; 0 = latest only")
    satellite_step_minutes: int = Field(
        20, ge=10, le=60, multiple_of=10,
        description="Satellite loop step. Clouds change slowly and satellite tiles are ~8x larger than radar tiles")
    cache_mb: float = Field(200, ge=10, description="Disk space for cached images; oldest are deleted first")


class Settings(BaseModel):
    data_dir: Path = Path("./data")
    host: str = "0.0.0.0"
    port: int = 8080
    https_port: int = Field(
        8443, ge=0, le=65535,
        description="Also serve the app over HTTPS on this port, so phones can share their GPS "
                    "(browsers require HTTPS for it); 0 = HTTP only")
    tls_cert: Path | None = Field(
        None, description="Your own HTTPS certificate (PEM, full chain), e.g. from 'tailscale cert'; "
                          "empty = the server makes one")
    tls_key: Path | None = Field(None, description="Private key for tls_cert (PEM)")
    tls_names: list[str] = Field(
        default_factory=list,
        description="Extra local names or addresses for the server-made certificate (.local, .lan, "
                    ".home.arpa, .internal or a private IP); its own addresses are found automatically")
    home: Position | None = Field(None, description="Fallback position when no GPS fix is available")
    demo: bool = Field(False, description="Serve synthetic data without touching the network")
    web_root: Path | None = None
    log_level: Literal["critical", "error", "warning", "info", "debug", "trace"] = "info"
    log_file: Path | None = Field(None, description="Also log to this file (rotated at 5 MB, 3 kept)")
    access_log: bool = Field(False, description="Log every HTTP request (noisy: phones poll every 5 s)")
    api_token: str | None = Field(
        None, description="If set, changes (settings, position, refresh) need this token. "
                          "Recommended when the Pi shares a network with strangers (marina Wi-Fi).")
    observation_retention_days: int = 60
    forecast: ForecastConfig = ForecastConfig()
    bandwidth: BandwidthConfig = BandwidthConfig()
    sources: SourcesConfig = SourcesConfig()
    sensors: SensorsConfig = SensorsConfig()
    boat: BoatProfile = BoatProfile()
    display: DisplayConfig = DisplayConfig()
    imagery: ImageryConfig = ImageryConfig()

    @field_validator("data_dir", "web_root", "log_file", "tls_cert", "tls_key", mode="before")
    @classmethod
    def _expand(cls, v):
        if v is None or v == "":
            return None
        return Path(os.path.expandvars(os.path.expanduser(str(v))))

    @field_validator("tls_names", mode="before")
    @classmethod
    def _names(cls, v):
        return [] if v is None or v == "" else v

    @model_validator(mode="after")
    def _https(self):
        if self.https_port and self.https_port == self.port:
            if "https_port" in self.model_fields_set:
                raise ValueError(f"https_port and port are both {self.port}; give HTTPS its own port (e.g. 8443) or 0 for none")
            self.https_port = 8444 if self.port == 9443 else 9443   # the web app already uses the default
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("tls_cert and tls_key go together: set both (your own certificate) or neither")
        return self

    def resolved_web_root(self) -> Path:
        if self.web_root:
            return Path(self.web_root)
        candidates = [
            Path(__file__).resolve().parents[2] / "web",  # repo / editable install
            Path.cwd() / "web",
            Path("/opt/shweather/web"),                   # deploy/install.sh layout
        ]
        if sys.platform == "win32":                       # deploy/windows/install.ps1 layout
            candidates.append(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "SHWeatherService" / "web")
        return next((c for c in candidates if c.is_dir()), candidates[0])


def system_config_path(platform: str = sys.platform, environ=None) -> Path:
    env = os.environ if environ is None else environ
    if platform == "win32":
        return Path(env.get("PROGRAMDATA", r"C:\ProgramData")) / "SHWeatherService" / "config.yaml"
    return Path("/etc/shweather/config.yaml")


def config_candidates(explicit: str | os.PathLike | None = None, platform: str = sys.platform,
                      environ=None) -> list[Path]:
    env = os.environ if environ is None else environ
    raw = [explicit, env.get("SHWEATHER_CONFIG"), "config.yaml", system_config_path(platform, env)]
    return [Path(c) for c in raw if c]


def find_config_file(explicit: str | os.PathLike | None = None) -> Path | None:
    return next((c for c in config_candidates(explicit) if c.is_file()), None)


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    cfg = find_config_file(path)
    data: dict = {}
    if cfg:
        # utf-8-sig: tolerate the byte-order mark Windows editors like to add
        with open(cfg, encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh) or {}
    settings = Settings.model_validate(data)
    if cfg:
        base = cfg.resolve().parent
        for name in ("data_dir", "web_root", "log_file", "tls_cert", "tls_key"):
            value = getattr(settings, name)
            if value is not None and not value.is_absolute():
                setattr(settings, name, base / value)
    return settings
