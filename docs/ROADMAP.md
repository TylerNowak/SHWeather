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

## Next (v0.4): verify and harden

- [ ] Run every provider against the live services and replace doc-based fixtures with
      recorded responses (see [DATA_SOURCES.md](DATA_SOURCES.md) verification table),
      including the radar and satellite picture sources
- [ ] Measure real data usage per refresh and publish typical MB/day for each mode
- [ ] Built-in HTTPS helper so phones get service-worker caching and GPS
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
