# Architecture

SHWeather is a self-hosted marine weather station that runs **on the boat**, on a
Raspberry Pi or a Windows PC. It pulls forecasts whenever it has a connection, keeps
working when it doesn't, and blends those forecasts with the boat's own instruments to
give a local, sailing-grade picture of what the wind, sea and sky are doing. Phones and
tablets use it through the web app it serves. (On Windows it installs under the name
SHWeatherService: folders, scheduled task and firewall rules.)

> **Not for navigation.** Model data is coarse near coasts and wrong sometimes. This
> software supplements, and never replaces, official forecasts, a working barometer and
> your own judgement.

## Design goals

1. **Offline-first.** The Pi spends most of its life with poor or no internet (cellular
   at the edge of range, marina Wi-Fi, Starlink with a data cap, nothing at all
   offshore). Every screen must render from local data and say how old that data is.
2. **Local over generic.** A forecast for a 25 km grid cell is a starting point. The
   boat's anemometer and barometer tell us how wrong it is *right here*, so we correct it.
3. **Sailing units and sailing questions.** Knots, true wind direction, gusts,
   significant wave height *and period*, swell vs wind-sea, pressure tendency, squall
   risk, reef/no-go thresholds for *your* boat.
4. **Light enough for a Pi, portable to Windows.** One Python process, SQLite, no build
   step for the web UI, no GRIB decoder, no native dependencies. Target: Pi 3B+ or newer
   (~80 MB RAM), or any Windows 10/11 PC.
5. **Worldwide, with regional upgrades.** Global models work everywhere; NOAA sources
   add detail in US coastal waters and the Great Lakes.

## System overview

```
                         ┌─────────────── when online ───────────────┐
                         │ Open-Meteo forecast   (wind, gust, MSLP,   │
                         │   visibility, CAPE; multi-model, global)   │
                         │ Open-Meteo marine     (waves, swell,       │
                         │   currents, SST; oceans only)              │
                         │ NWS api.weather.gov   (alerts, marine zone │
                         │   text, gridpoint waves incl. Great Lakes) │
                         │ NDBC latest_obs       (buoy observations)  │
                         │ NOAA CO-OPS           (tides, tidal curr.) │
                         │ RainViewer / IEM      (radar pictures)     │
                         │ NASA GIBS             (satellite IR)       │
                         │   ...only while the Radar tab is open      │
                         └──────────────────┬─────────────────────────┘
                                            │ httpx, retry/backoff
┌──── boat network ────┐          ┌─────────▼──────────────────────────────┐
│ NMEA 0183 TCP/UDP    │─────────▶│              shweather (Pi)             │
│ NMEA 0183 serial/USB │─────────▶│  sensors/  → SensorHub (latest values,  │
│ Signal K server      │─────────▶│              true-wind, 1-min averages) │
│ BME280 on I²C        │─────────▶│  providers/→ normalised hourly series   │
└──────────────────────┘          │  analysis/ → tendency, Zambretti,       │
                                  │              nowcast, go/no-go          │
                                  │  SQLite    → cache, forecast grids,     │
                                  │              observations, positions    │
                                  │  FastAPI   → /api/*  +  static PWA      │
                                  └─────────▲──────────────────────────────┘
                                            │ HTTP on the boat LAN
                               phones / tablets / chartplotter browser (PWA)
```

## Key decisions

| Decision | Choice | Why |
|---|---|---|
| Server | Python 3.11+, FastAPI, uvicorn | Best weather/marine library ecosystem; OpenAPI schema for the phone app for free. |
| Storage | SQLite (WAL) | Zero admin, survives power cuts, one file to back up. Writes are batched once a minute to spare the SD card. |
| Forecast transport | Open-Meteo JSON (not GRIB) | GRIB decoding needs eccodes (large native dependency). Open-Meteo accepts many coordinates per request, so we fetch a small grid of points as JSON and interpolate locally. GRIB export for OpenCPN is on the roadmap. |
| Web UI | Vanilla JS PWA, no build step | Anyone can edit it on the Pi itself; service worker gives offline caching; installable on iOS/Android. Native wrapper (Capacitor) later if needed. |
| Instruments | NMEA 0183 + Signal K REST + BME280 | NMEA 0183 covers most multiplexers and older instruments; Signal K (e.g. OpenPlotter) already bridges NMEA 2000. A $5 BME280 gives any boat a barograph. |
| Units | Canonical internal units, convert in the UI | See below. |

### Canonical units

Everything stored and served by the API uses:

| Quantity | Unit | Field suffix |
|---|---|---|
| Wind and current speed | knots | `_kn` |
| Directions | degrees true, "from" for wind/waves, "towards" for current | `_deg` |
| Pressure | hPa, reduced to mean sea level | `_hpa` |
| Wave height, sea level | metres | `_m` |
| Wave period | seconds | `_s` |
| Temperature | °C | `_c` |
| Visibility | metres | `visibility_m` |
| Time | Unix seconds, UTC | `time` / `ts` |

The API never converts. The PWA converts at display time (`web/js/units.js`) into the
device's unit system:

| Quantity | Metric | Imperial | + Nautical (either system) |
|---|---|---|---|
| Wind, current | km/h | mph | knots |
| Waves, tides | m | ft | |
| Temperature | °C | °F | |
| Pressure | hPa | inHg | |
| Rain | mm | in | |
| Distance, visibility | km | mi | nautical miles |

Each device starts from the server's `display` default (or its region when that is
`auto`), can switch system with one tap, and can override single quantities. Messages
built on the server (go/no-go reasons, barometer warnings) carry a code plus canonical
values next to the English text, so the app words them in the chosen units.

## Platforms

| | Raspberry Pi OS / Linux | Windows 10/11 |
|---|---|---|
| Install | `deploy/install.sh` | `deploy/windows/install.ps1` |
| Runs as | systemd service, user `shweather` | Scheduled task at boot as SYSTEM, supervised by `run-service.ps1` (restart with backoff) |
| Code | `/opt/shweather` | `C:\Program Files\SHWeatherService` |
| Config / data | `/etc/shweather`, `/var/lib/shweather` | `C:\ProgramData\SHWeatherService` (`config.yaml`, `data\`, `logs\`) |
| Logs | journald | rotating `logs\shweather.log` (`log_file` setting) |
| Serial NMEA | `/dev/ttyUSB0` | `COM3` |
| BME280 (I2C) | yes | no (use NMEA or Signal K pressure) |
| Firewall | n/a (Pi OS default) | Inbound rule group `SHWeatherService` |

Portability rules in the code: `pathlib` everywhere; relative paths in the config are
resolved against the config file (a Windows service starts in `System32`); `~` and
environment variables expand in paths; no POSIX-only modules; the database is closed on
shutdown (Windows cannot replace open files). CI runs the test suite on Linux and Windows.

## Offline strategy

**Forecast grid.** On every refresh the Pi requests a grid of points around the boat
(default 5 × 5 at 0.25°, about ±30 nm) from both Open-Meteo APIs and stores each point's
hourly series. Queries for *any* position inside that box are answered by
inverse-distance interpolation (wind and wave directions are interpolated as vectors).
So the forecast keeps following the boat for hours after the connection drops.

**Passage download.** Before leaving, `POST /api/refresh?radius_nm=150` fetches a wider,
coarser grid (capped at 400 points) covering the whole passage.

**Refresh policy.** The scheduler refreshes when the newest grid is older than
`refresh_minutes`, the boat has moved more than `refetch_distance_nm` from the grid
centre, or it is near the grid's edge. Failures back off exponentially (5 → 60 min), per
source, so one dead service never delays the others.

**Bandwidth limiter.** Every download goes through one HTTP client (`net.py`) that
enforces the `bandwidth` settings, which are editable in config.yaml and at runtime from
the app (`PUT /api/bandwidth`, persisted in SQLite):

- *Speed cap* (`max_kbps`): responses are streamed through a token bucket shared by all
  concurrent downloads, so the total rate stays under the cap and a slow cellular or
  satellite link stays usable for the crew.
- *Data caps* (`daily_mb`, `monthly_mb`): wire bytes (after gzip) are counted per UTC day.
- *Alert reserve* (`alert_reserve_pct`): ordinary fetches stop at, say, 90% of a cap;
  requests marked essential (NWS alerts, a few KB each) may use the rest, so a used-up
  budget never hides a gale warning.
- *Data saver* (`saver`): 3×3 grid, 3-day forecast, no NWS gridpoint where ocean wave
  data exists, and fetch intervals stretched 1.5-4×.

**Forecast tab.** A traditional forecast (today, the next 24 hours, then one row per day
with the high, the low, the sky and the chance of rain) built on the phone from the same
hourly forecast the Wind tab uses (`GET /api/forecast?hours=384&past_hours=24`), so it
works offline and costs no extra download. `web/js/daily.js` does the work in plain
functions (tested in Node):

- *Conditions* come from the WMO weather code of each hour (Open-Meteo), with sky cover
  refined by cloud %. A day or a period takes thunder if any hour has it, else rain, snow
  or drizzle if it lasts two hours or more, else fog if it lasts a quarter of the time
  ("Morning fog, then sunny"), else the average cloud cover of the daylight hours.
  Chances are worded the NWS way from the probability of precipitation: slight chance
  (<25%), chance (<55%), likely (<75%), then plain "Rain".
- *Days* are the phone's calendar days, midnight to midnight; today keeps its past hours
  so its high and low cover the whole day. A later day is listed only if the forecast
  reaches its afternoon, so how many days appear follows `forecast.forecast_days` (5 by
  default, 3 in data-saver mode, up to 16).
- *Periods* (day 06-18, night 18-06) are worded like a forecaster's text: sky or
  precipitation with its timing, high or low, wind range with a change of direction
  ("SW 10 to 15 kn, becoming NW") and gusts, rain total. The worst go / reef / no-go hour
  of each day comes from the server's assessment.
- *Sunrise and sunset* use the NOAA sunrise equation at the forecast position; *feels
  like* is the NWS wind chill or heat index.

**Radar tab.** One north-up map (Web Mercator, drawn on a canvas) around the boat, with a
timeline that runs from the last hour of observations into the forecast:

- *Offline layers* come from data the server already has: wind arrows (coloured by the
  boat's reef and no-go limits), cloud cover and rain from the stored forecast grid
  (`GET /api/map`, interpolated bilinearly in space and linearly in time, wind as vectors),
  the latest buoy reports, and a land-and-lakes map built from Natural Earth
  (`web/data/basemap.json`, 119 KB gzipped, regenerated by `tools/build_basemap.py`).
- *Live layers* are radar mosaics and infrared satellite pictures (`imagery.py`). The
  server lists frames (`GET /api/imagery`) and proxies tiles (`GET /api/tiles/...`),
  downloading each one once, through the bandwidth limiter, and caching it on disk for
  every phone. Nothing is downloaded until someone opens the tab; a request may trigger a
  download only with the app's `X-SHW-Client` header (and token), anyone may read the
  cache. Tiles are refused beyond the source's zoom or 600 nm from the boat. Data-saver
  mode offers only the latest picture; a reached data cap pauses the live layers and the
  forecast layers carry on. Cached pictures are deleted after 6 hours or when over
  `imagery.cache_mb`.
- Infrared pictures cover everything, ground included. GIBS uses an enhanced scale: grey
  that brightens as it gets colder, then colours for the coldest cloud tops. The app turns
  that into a cloud mask (coloured = solid cloud, grey = more opaque the colder it is,
  warm ground clear) and tints it for the theme. Low warm cloud and fog look like clear
  sky in infrared. The satellite loop uses 20-minute steps: clouds change slowly and a
  satellite tile is about 8× the size of a radar tile.

**Staleness is always visible.** Every response carries `fetched_at` and `age_s`, and
the UI turns the forecast badge amber after 6 h and red after 24 h.

## Local ("nowcast") forecasting

The Pi turns forecast + instruments into a local forecast with three cheap techniques:

1. **Bias-corrected forecast.** Over the last 6 hours we compare 1-minute instrument
   averages with the forecast for the same hours (the fetch includes `past_days=1`). The
   median speed ratio, circular-mean direction offset and mean pressure offset are
   applied to the next 12 hours, decaying with a 3 h time constant. On a lake where the
   model always under-forecasts the afternoon sea breeze, this makes the next few hours
   noticeably better.
2. **Pressure tendency.** 3-hour change from the onboard barometer, classified with the
   standard terms (steady / slowly / quickly / very rapidly), plus the classic sailor's
   warnings (a fall of ≥ 3.6 hPa in 3 h: gale possible).
3. **Zambretti forecaster.** A 1915 barometric algorithm (pressure, trend, wind
   direction, season). Crude, but it works with **no internet at all**, which is what
   matters mid-passage. It is labelled as a heuristic in the UI.

## Region-specific sources

| Waters | Wind / sky | Waves | Official text & alerts | Observations | Tides |
|---|---|---|---|---|---|
| Anywhere | Open-Meteo (best_match: HRRR/GFS/ECMWF/ICON…) | Open-Meteo marine (ECMWF WAM, MFWAM, GFS-Wave) | — | onboard | — |
| US coastal / Gulf | + NWS gridpoints | + NWS gridpoints | NWS alerts, CWF/NSH zone text | NDBC buoys + C-MAN | CO-OPS tides & currents |
| Great Lakes | + NWS gridpoints | **NWS gridpoints** (ocean wave models do not cover the lakes) | NWS alerts, NSH/GLF zone text | NDBC buoys (seasonal) | none (seiches only) |
| Inland lakes | Open-Meteo (`cell_selection=sea` picks water cells) | — | NWS alerts in the US | onboard | — |
| Offshore / international | Open-Meteo global models | Open-Meteo marine | — (roadmap: OPC/NHC high-seas text) | onboard | — |

Each source is independent: one failing (or not applying, e.g. NWS outside the US)
never blocks the others. `/api/status` reports each source's last success and error.

## Code layout

```
server/shweather/
  app.py          FastAPI app factory, lifespan (starts sensors + scheduler), static PWA
  api.py          REST routes
  config.py       YAML config → pydantic models
  db.py           SQLite schema + helpers
  service.py      Orchestration: refresh, forecast_at(), conditions snapshot
  scheduler.py    Background refresh / aggregation / pruning loops
  net.py          Shared HTTP client: User-Agent, speed cap, data caps, alert reserve
  series.py       Canonical hourly series fields and helpers
  geo.py          Distances, grids, inverse-distance interpolation
  units.py        Unit conversion helpers
  imagery.py      Radar/satellite frame lists and the tile proxy + disk cache (Radar tab)
  demo.py         Synthetic data transport for offline demos and UI work
  demo_imagery.py Synthetic radar and satellite tiles (small PNG encoder) for demo mode
  simulator.py    NMEA 0183 sentence generator (sail without a boat)
  providers/      open_meteo, nws, ndbc, coops: fetch + normalise
  sensors/        nmea0183 parser, readers (tcp/udp/serial/file), signalk, bme280, hub
  analysis/       beaufort, pressure, zambretti, wind (true wind), nowcast, conditions
web/              SHWeather PWA (index.html, js/, css/, sw.js, manifest); js/app.js = Wind tab + routing,
                  js/forecast.js + js/daily.js = Forecast tab, js/radar.js = Radar tab,
                  js/units.js = unit systems, data/basemap.json = offline land map
tools/            build_basemap.py (Natural Earth -> web/data/basemap.json)
deploy/           systemd unit, Raspberry Pi install script
deploy/windows/   install.ps1, run-service.ps1 (supervisor), shweather-service.ps1, uninstall.ps1
tests/            pytest suite with recorded-format fixtures; tests/web: Node tests for the PWA
```

## API (v0)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Version, position and its source, per-source health, sensor freshness, data usage |
| GET | `/api/now` | Cockpit snapshot: instruments + forecast now + tendency + Zambretti + alerts + assessment |
| GET | `/api/forecast?lat=&lon=&hours=&past_hours=` | Hourly interpolated forecast, local-corrected, with per-hour go/no-go (Wind and Forecast tabs) |
| GET | `/api/observations?metrics=&hours=` | Onboard time series (barograph, wind history) |
| GET | `/api/alerts` | Active NWS alerts for the position |
| GET | `/api/marine-text` | NWS marine zone forecast text |
| GET | `/api/buoys` | Nearest NDBC observations |
| GET | `/api/tides` | Nearest tide station high/low and tidal-current predictions |
| POST | `/api/position` | Set position from a phone's GPS when the Pi has none |
| POST | `/api/refresh?radius_nm=` | Force refresh / passage download |
| GET/PUT | `/api/boat` | Boat profile (reef and no-go thresholds) |
| GET/PUT | `/api/bandwidth` | Bandwidth limits, today's/this month's usage, 31-day history |
| GET | `/api/map?past_hours=&hours=` | Model wind, gusts, cloud and rain at every forecast grid point (Radar tab) |
| GET | `/api/imagery` | Radar and satellite frames on offer, zoom limits, attribution, pause/saver state |
| GET | `/api/tiles/{radar,satellite,basemap}/{frame}/{z}/{x}/{y}.png` | A cached or freshly downloaded tile |

Interactive docs are served at `/docs` (OpenAPI). Write endpoints (POST/PUT) require an
`X-SHW-Client` header (CSRF guard) and, when `api_token` is configured, the token in
`X-SHW-Token` or `Authorization: Bearer`.
