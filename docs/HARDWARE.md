# Hardware

SHWeather runs on a **Raspberry Pi** (the lightest option) or on any **Windows 10/11
PC** already aboard, such as a nav-station PC running OpenCPN or a small fanless mini PC.

## Raspberry Pi

| Board | Verdict |
|---|---|
| Pi 5 / Pi 4 (2 GB+) | Recommended. Room for Signal K / OpenPlotter alongside. |
| Pi 3B+ | Fine for SHWeather alone. |
| Pi Zero 2 W | Works (512 MB RAM); keep the forecast grid at the default size. |

Expected footprint: one Python process, roughly 60-90 MB RAM, near-idle CPU except for a
few seconds per forecast refresh.

## Windows PC

| Host | Notes |
|---|---|
| Fanless mini PC (Intel N100 class) | 6-10 W, runs on 12 V with the right supply; plenty of headroom for OpenCPN too. |
| Laptop / nav-station PC | Works; make sure it never sleeps while you rely on it. |

Setup is in [DEPLOYMENT.md](DEPLOYMENT.md#windows-10--11). Things that differ from a Pi:

- **Sleep and updates.** Disable sleep on AC power (`powercfg /change standby-timeout-ac 0`).
  After a Windows Update restart the service starts again by itself.
- **Serial ports** are `COM3`, `COM4`... (Device Manager > Ports). Install the adapter's
  driver (FTDI, Prolific or CH340) if Windows doesn't. Close other programs that hold
  the port: only one program can open a COM port at a time. Share instruments through
  OpenCPN's or Signal K's network output instead.
- **No I2C bus**, so no BME280; get barometric pressure from an instrument that sends
  NMEA MDA/XDR sentences or through Signal K.
- **Power draw** is higher than a Pi's 3-6 W, which matters on a battery bank at anchor.

## Power

- Use a quality **12 V to 5 V buck converter** rated for the Pi (5.1 V, 3 A for Pi 4,
  5 A for Pi 5), fused on the 12 V side. Cheap cigarette-lighter adapters sag when the
  engine starts and corrupt SD cards.
- Consider a small UPS HAT, or at least a clean shutdown button: SQLite survives power
  cuts, but SD cards are less forgiving.

## Storage

Use a high-endurance microSD card or, on Pi 4/5, a USB or NVMe SSD. SHWeather
writes sensor data once a minute in batches, so the database stays small (tens of MB for
60 days of observations). The Radar tab's picture cache adds up to `imagery.cache_mb`
(200 MB by default); pictures older than 6 hours are deleted.

## Instruments

### NMEA 0183

- **Wi-Fi or Ethernet multiplexers** (e.g. from Yacht Devices, Quark-elec, Digital
  Yacht) usually serve NMEA 0183 on **TCP or UDP port 10110**. Configure a `tcp` or `udp`
  source.
- **Wired instruments**: NMEA 0183 is RS-422 (differential). Use an **opto-isolated
  USB-RS422 adapter** and a `serial` source (`pip install 'shweather[serial]'`).
  **Never wire NMEA directly to the Pi's GPIO pins**: voltages and ground differences on a
  boat will destroy it.
- A **USB GPS puck** shows up as `/dev/ttyACM0` (usually 9600 baud) and outputs NMEA:
  add it as a `serial` source.

Sentences used: RMC, GGA, GLL, VTG (position, SOG, COG), HDT, HDG, HDM, VHW (heading,
speed through water), MWV, VWR (apparent or true wind), MWD (true wind direction), MDA
(barometer, temperatures, humidity, wind), MTW (water temperature), XDR (pressure,
temperature, humidity transducers). Everything else, including AIS, is ignored.

### NMEA 2000 and Signal K

The simplest route for NMEA 2000 is a **Signal K server** (for example via
[OpenPlotter](https://openmarine.net/openplotter)) with a CAN HAT such as PICAN-M, or a
USB gateway. Point SHWeather at it with the `signalk` section; it polls the REST
API every 2 s. If Signal K's derived-data plugin computes true wind, that is used directly.

### True wind

Forecasts predict wind over the ground. If your instruments only report apparent wind,
SHWeather derives true wind from apparent wind plus SOG/COG (or STW and heading),
the same maths as a chartplotter. With no heading sensor it assumes heading = COG, which
is fine except when moving slowly in a strong current.

## Barometer (strongly recommended)

A barometer is the single most useful sensor for weather at sea. The pressure tendency and
the offline Zambretti forecast need one, and it lets the local correction calibrate the
model.

### BME280 on I2C (about $5)

Wiring on the Pi header:

| BME280 | Pi pin |
|---|---|
| VIN | 3V3 (pin 1) |
| GND | GND (pin 6) |
| SCL | GPIO3 / SCL (pin 5) |
| SDA | GPIO2 / SDA (pin 3) |

Enable I2C (`sudo raspi-config` → Interface Options → I2C), check with `i2cdetect -y 1`
(address 0x76 or 0x77), install with `sudo ./deploy/install.sh --bme280`, and add the
`bme280:` section to the config.

Mount it inside the cabin, away from the engine, stove and direct sun. Pressure inside a
cabin equals outside pressure; temperature and humidity don't, so those are reported as
*cabin* values.

### Altitude and calibration

Forecasts use **mean-sea-level** pressure. On the Great Lakes and inland lakes the water
itself is well above sea level, so set `sensors.pressure_altitude_m` to the lake's
elevation plus the sensor's height above the water:

| Lake | Approx. surface elevation |
|---|---|
| Superior | 183 m |
| Michigan-Huron | 176 m |
| Erie | 174 m |
| Ontario | 75 m |

Then compare against a nearby official observation on a calm day and put any remaining
difference in `pressure_offset_hpa`. The local correction also learns a pressure offset
automatically, but a calibrated sensor makes the tendency warnings more trustworthy.

## Networking on board

- Put the Pi on the boat's router, or let it run its own access point (OpenPlotter can
  do this).
- Internet comes from whatever the boat has: marina Wi-Fi, a cellular router, Starlink.
  If that link is metered or shared, set the **bandwidth** limits (speed cap, daily and
  monthly caps, data saver) in the config or in the app.
