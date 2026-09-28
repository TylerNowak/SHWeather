# Data sources

Each source is independent: one failing, or not applying at the boat's position, never
blocks the others. `/api/status` and the footer of the app show each source's health.

| Source | What we use | Coverage | Default cadence | Terms |
|---|---|---|---|---|
| [Open-Meteo forecast API](https://open-meteo.com/en/docs) | Wind, gusts, direction (kn), MSL pressure, temperature, dew point, humidity, precipitation, cloud, visibility, CAPE, weather code, day/night | Global; `best_match` picks HRRR/GFS/ECMWF/ICON/GEM etc. per location | Every 60 min (grid of 25 points) | Free for non-commercial use, CC BY 4.0 attribution; API key for commercial; self-hostable |
| [Open-Meteo marine API](https://open-meteo.com/en/docs/marine-weather-api) | Significant wave height/direction/period, wind-sea, primary swell, ocean current, sea surface temperature | Oceans (ECMWF WAM, MFWAM, GFS-Wave, ...). **Not** the Great Lakes or inland lakes. Coarse near coasts. | With the forecast | As above |
| [NWS API](https://www.weather.gov/documentation/services-web-api) `/points`, `/gridpoints` | Forecaster-edited wind and **wave grids**, including the **Great Lakes** | US land and coastal/lake waters | With the forecast | Public domain; requires a User-Agent with contact |
| NWS `/alerts/active?point=` | Small Craft Advisory, Gale/Storm Warnings, Special Marine Warnings, ... | US | Every 10 min (uses the bandwidth reserve) | Public domain |
| NWS `/zones`, `/products` (NSH, CWF, GLF) | Text forecast for the boat's marine zone | US coastal and Great Lakes zones | Every 60 min | Public domain |
| [NDBC](https://www.ndbc.noaa.gov/) `latest_obs.txt` | Nearest buoy / C-MAN observations: wind, gusts, waves, pressure + tendency, temperatures | Mostly US and partner platforms worldwide; many Great Lakes buoys are seasonal (spring to autumn) | Every 30 min | Public domain |
| [NOAA CO-OPS](https://api.tidesandcurrents.noaa.gov/api/prod/) | Tide high/low and hourly heights (MLLW), tidal current max/slack | US coasts (none on the Great Lakes) | Every 6 h | Public domain |

### Radar tab pictures

Downloaded only while someone has the Radar tab open (see `imagery:` in the config).

| Source | What we use | Coverage | Cadence | Terms |
|---|---|---|---|---|
| [RainViewer Weather Maps API](https://www.rainviewer.com/api.html) (default radar) | Past 2 h of radar mosaic tiles at 10-min steps, "Universal Blue" colours, zoom ≤ 7 | Worldwide where national radars exist | Frame list every 5 min while the tab is open | Free for personal and educational use with attribution ("Weather data by RainViewer"); 100 requests/min per IP. Since 1 Jan 2026 the free tier has no satellite, no nowcast and stops at zoom 7 ([transition FAQ](https://www.rainviewer.com/api/transition-faq.html)). Any RainViewer-compatible server works, e.g. a self-hosted [LibreWXR](https://github.com/JoshuaKimsey/LibreWXR), which can add satellite frames and nowcasts. |
| [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/GIS/ridge.phtml) NEXRAD composite (`imagery.radar: iem`) | `ridge::USCOMP-N0Q-<UTC time>` tiles, 5-min composites | US and adjacent waters, including the Great Lakes | Frames computed from the clock; no list download | NOAA data served as-is by Iowa State; fine for a single boat, not for thousands of users |
| [NASA GIBS](https://nasa-gibs.github.io/gibs-api-docs/) (default satellite) | Band 13 "clean" infrared from GOES-East, GOES-West or Himawari, whichever is nearest the boat; images every 10 min, the loop uses every 20 min | Americas, Atlantic west of ~5° W, Pacific, East Asia, Australia. **Not** Europe, Africa or the Indian Ocean (no Meteosat layer) | The newest image is found by trying the tile under the boat (GIBS publishes with a delay) | Open NASA data; credit NASA GIBS and the satellite |
| [Natural Earth](https://www.naturalearthdata.com/) 1:50m land and lakes | Offline land map, bundled in the app | Worldwide | Never downloaded | Public domain |

## Great Lakes specifics

- Open-Meteo's wave models are ocean models; over the lakes they return nothing. The app
  fills wave height, period and direction from the NWS gridpoint forecast at the boat's
  position (within 20 nm), which is produced by the local NWS offices' Great Lakes wave
  guidance. The forecast response lists these fields in `filled_from_nws`.
- No tides: the CO-OPS lookup finds no station and the card stays hidden. Seiches and
  wind set-up are not modelled yet (see the roadmap).
- Barometers must be reduced to sea level (`sensors.pressure_altitude_m`), see
  [HARDWARE.md](HARDWARE.md).

## Verification status (v0.1)

The build environment for v0.1 could not reach these services directly, so parsers were
written against the documented formats and tested with fixtures. Where live responses
could be fetched, they became the fixtures.

| Source | Checked against | Status |
|---|---|---|
| NDBC `latest_obs.txt`, `realtime2/*.txt` | Live files (Sep 2026), rows copied into `tests/fixtures` | ✅ parser tested on real data |
| CO-OPS `currents_predictions` | Live response (Sep 2026) | ✅ |
| CO-OPS `predictions` (hilo/hourly), `mdapi` stations | Documentation | ⚠️ needs a live check |
| Open-Meteo forecast + marine | Documentation (parameter and variable names, units, multi-location arrays) | ⚠️ needs a live check |
| NWS points, gridpoints, alerts, zones, products | Documentation (api.weather.gov blocks automated doc fetchers) | ⚠️ needs a live check; zone lookup by point is the least certain |
| RainViewer `weather-maps.json` + tiles | Live, on the Great Lakes (28 Sep 2026): 7-frame loops, zoom 6 and 7 tiles | ✅ |
| IEM `ridge::USCOMP-N0Q-*` tiles | IEM documentation and a project that switched to these names in 2026 | ⚠️ needs a live check; the 10-min publication delay is an estimate |
| NASA GIBS geostationary infrared | Live, GOES-East (28 Sep 2026): tile matrix `GoogleMapsCompatible_Level6`, images about 30 min behind real time, found by probing | ✅ GOES-East; GOES-West and Himawari use the same code path, not yet seen live |

To check on a machine with internet: `shweather fetch --lat 41.9 --lon -87.5` prints each
source's status; any `error` entries point at a format mismatch.

## Bandwidth

All downloads go through one HTTP client that enforces the `bandwidth` settings: a
shared speed cap (kbit/s), daily and monthly caps counted in wire bytes (after gzip), and
a reserve so NWS alerts keep flowing when a cap is hit. Data-saver mode shrinks the grid to
3×3 points and 3 days, skips the NWS gridpoint where ocean wave data exists, and spaces
fetches 2-4× further apart. The app's Settings show today's and this month's usage.

Radar tab pictures, measured on a real install (Great Lakes, Sep 2026): radar tiles
average 7 KB (up to about 30 KB in heavy rain), infrared satellite tiles 52 KB (up to
125 KB). A phone showing a 50 nm range needs about 6 radar and 4 satellite tiles per
frame, so opening the tab with the default loop (7 radar frames at 10 minutes, 4 satellite
frames at 20 minutes) costs about 1 MB, then about 0.3 MB per 20 minutes while it stays
open. A large desktop window shows many more tiles (about 40 radar and 25 satellite):
about 7 MB to open, then about 1.9 MB per 20 minutes. Data-saver mode fetches only the
latest picture of each (about 0.3 MB on a phone). `imagery.satellite_step_minutes` and
`imagery.loop_minutes` trade loop detail for data; `imagery.enabled: false` downloads
none of it.
