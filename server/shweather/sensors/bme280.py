"""Bosch BME280 on the Pi's I2C bus: a $5 barograph.

Wiring (Pi header): VIN->3V3 (pin 1), GND->pin 6, SCL->GPIO3 (pin 5), SDA->GPIO2 (pin 3).
Enable I2C with ``sudo raspi-config`` -> Interface Options -> I2C, then check the address
with ``i2cdetect -y 1`` (0x76 or 0x77).

Mounted inside the cabin the pressure is fine (cabins are not airtight), but temperature
and humidity describe the cabin, not the weather, so they are reported as cabin_* metrics.
Install the driver with ``pip install 'shweather[bme280]'``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from ..config import Bme280Config
from .hub import SensorHub

log = logging.getLogger(__name__)


async def run_bme280(cfg: Bme280Config, hub: SensorHub) -> None:
    name = f"bme280:i2c-{cfg.i2c_bus}@0x{cfg.address:02x}"
    if not sys.platform.startswith("linux"):
        msg = "BME280 needs a Linux I2C bus (Raspberry Pi); on Windows use a barometer via NMEA or Signal K"
        hub.mark_source(name, connected=False, error=msg)
        log.error(msg)
        return
    try:
        import bme280  # type: ignore
        import smbus2  # type: ignore
    except ImportError:
        hub.mark_source(name, connected=False, error="driver missing: pip install 'shweather[bme280]'")
        log.error("BME280 configured but driver missing: pip install 'shweather[bme280]'")
        return
    backoff = 5.0
    while True:
        try:
            bus = smbus2.SMBus(cfg.i2c_bus)
            cal = await asyncio.to_thread(bme280.load_calibration_params, bus, cfg.address)
            while True:
                data = await asyncio.to_thread(bme280.sample, bus, cfg.address, cal)
                st = hub.sources.setdefault(name, {"sentences": 0, "useful": 0})
                st.update(connected=True, last_seen=hub.clock())
                st["sentences"] += 1
                st["useful"] += 1
                hub.update({"pressure_hpa": data.pressure, "cabin_temp_c": data.temperature,
                            "cabin_humidity_pct": data.humidity}, name)
                await asyncio.sleep(cfg.interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            hub.mark_source(name, connected=False, error=str(exc))
            log.warning("BME280: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)
