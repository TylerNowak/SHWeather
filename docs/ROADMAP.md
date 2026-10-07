# Roadmap

## v0.1

- FastAPI server, SQLite store, offline forecast grid with interpolation
- Open-Meteo forecast + marine, NWS alerts / gridpoint waves / zone text, NDBC, CO-OPS
- NMEA 0183 (TCP/UDP/serial/file), Signal K, BME280; true-wind derivation
- Pressure tendency + warnings, Zambretti, local (nowcast) correction, boat-limit assessment
- Bandwidth limiter: speed cap, daily/monthly caps, alert reserve, data saver
- PWA cockpit display with day/dark/night themes; demo mode; NMEA simulator; install script

## v0.2

- Windows 10/11 hosting: installer, boot-time service with supervisor, firewall rules,
  management and uninstall scripts; CI on Windows
- Metric / Imperial switching with a Nautical option and per-unit overrides; server default
  for the boat; go/no-go reasons and barometer warnings worded in the chosen units
- App renamed **SHWeather**

## v0.3

- **Radar tab**: map around the boat with radar (RainViewer worldwide, or NOAA NEXRAD via
  IEM) and infrared satellite (NASA GIBS) loops, then forecast rain, cloud and wind arrows
  for 48 h on one timeline; buoy reports; tap for values; offline land map (Natural Earth).
  The server downloads and caches the pictures only while the tab is open, within the
  bandwidth caps.
- Windows installer: port conflicts found before installing, `-Port` moves an existing
  install, `status` diagnoses why phones can't connect (Public network, firewall Block
  rules, missing rule)
- Text responses gzip-compressed

## v0.4

- **Forecast tab**: a traditional forecast for the boat's position. Now (sky, temperature,
  feels like, today's high and low, today / tonight in words), the next 24 hours with
  sunrise and sunset, and a row per day (up to 16, as many as the server downloads) with
  high and low on a range bar, the sky, the chance of rain, wind and the day's worst
  go / reef / no-go; each day opens into day and night text, four parts of the day and
  the day's facts. Worked out on the phone from the stored forecast, so it works offline.
- The **Weather** tab is now **Wind** (old `#weather` links still open it)
- Demo mode plays out five days: a cold front, a clear high with morning fog, a warm-up
  and a rainy low
- Wind tab charts scale to the hours on screen

## v0.5

- **Phone GPS**: Settings > Position > *Use this device's location* polls the phone's or
  tablet's GPS while the app is open, at a rate you choose (10 s to 1 h), and sends each
  fix to the server; the boat's own GPS still comes first. Rough (IP-based) fixes are
  ignored, the header says where the position comes from, and a banner says when the
  browser blocks location.
- **Built-in HTTPS** (`https_port`, 8443): the server makes its own CA (name-constrained
  to the local network) and certificate, renews it when its addresses change, and offers
  the CA for phones to install. Pure Python, no new dependencies. Windows installer opens
  the port (`-HttpsPort`).

## Next (v0.6): verify and harden

- [ ] Run every provider against the live services and replace doc-based fixtures with
      recorded responses (see [DATA_SOURCES.md](DATA_SOURCES.md) verification table),
      including the radar and satellite picture sources
- [ ] Measure real data usage per refresh and publish typical MB/day for each mode
- [x] Built-in HTTPS so phones get service-worker caching and GPS (v0.5)
- [ ] Test on real hardware: Pi Zero 2 W memory, a multiplexer, a BME280, Signal K
- [ ] Test the Windows installer on a real Windows 10 and 11 machine (CI only parses it)
- [ ] Optional: a real Windows service (services.msc) instead of a scheduled task
- [ ] Pressure alarm output (GPIO buzzer / notification) for rapid falls

## Later

- **Passage planning:** forecast along a route (GPX import, departure-time sweep)
- **Offshore without internet:** request GRIBs by email over satellite (Saildocs-style
  requests via Iridium GO / Starlink roaming) and decode them on the Pi
- **GRIB export** of the stored grid for OpenCPN and other chartplotters
- **Model spread:** fetch 2-3 models and show their disagreement as uncertainty
- **Great Lakes:** NOAA GLOFS currents and water levels, seiche / storm-surge hints
- **Radar tab:** Meteosat imagery for Europe/Africa (EUMETSAT view service), lightning,
  radar nowcast from a self-hosted LibreWXR, pre-download of pictures along a passage
- **High-seas text** (OPC/NHC) and non-US national marine services (Met Office shipping
  forecast, Environment Canada, Météo-France)
- **Native companion app:** Capacitor wrapper for background alerts and app stores
- **NMEA 2000 directly** via python-can for boats without Signal K
- Translations; metric/imperial defaults by locale
