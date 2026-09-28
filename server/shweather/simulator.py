"""NMEA 0183 boat simulator: sail without a boat.

Generates RMC, HDT, VHW, MWV (apparent) and MDA (barometer, temperatures) sentences for a
boat beating back and forth near a position. Use it to develop against a realistic data
stream:

    shweather simulate --udp 127.0.0.1:10110      # then configure a udp source on port 10110
    shweather simulate --tcp 10110                # serves NMEA to TCP clients (like a multiplexer)
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time

from .geo import bearing_deg, distance_nm
from .sensors.nmea0183 import with_checksum

log = logging.getLogger(__name__)


def _lat(v: float) -> tuple[str, str]:
    d = int(abs(v))
    return f"{d:02d}{(abs(v) - d) * 60:07.4f}", "N" if v >= 0 else "S"


def _lon(v: float) -> tuple[str, str]:
    d = int(abs(v))
    return f"{d:03d}{(abs(v) - d) * 60:07.4f}", "E" if v >= 0 else "W"


class BoatSim:
    def __init__(self, lat: float, lon: float, *, scenario=None, bias: dict | None = None,
                 stw_kn: float = 5.5, tack_every_s: float = 900, radius_nm: float = 3.0, seed: int = 7):
        self.home = (lat, lon)
        self.lat, self.lon = lat, lon
        self.scenario = scenario
        self.bias = bias or {"ratio": 1.0, "dir_offset": 0.0, "pressure_offset": 0.0}
        self.stw = stw_kn
        self.tack_every = tack_every_s
        self.radius = radius_nm
        self.rng = random.Random(seed)
        self.t0 = time.time()
        self.tack = 1

    def weather(self, t: float) -> dict:
        if self.scenario:
            r = self.scenario.at(t, self.lat, self.lon)
            return {"tws": r["wind"] * self.bias["ratio"], "twd": (r["dir"] + self.bias["dir_offset"]) % 360,
                    "p": r["pressure"] + self.bias["pressure_offset"], "air": r["temp"], "water": r["sst"],
                    "rh": r["rh"], "dew": r["dew"]}
        el = t - self.t0
        return {"tws": 12 + 3 * math.sin(el / 600), "twd": (225 + 10 * math.sin(el / 900)) % 360,
                "p": 1013 - el / 3600 * 0.6, "air": 20.0, "water": 18.0, "rh": 70.0, "dew": 14.3}

    def step(self, t: float, dt_s: float) -> list[str]:
        w = self.weather(t)
        gustiness = 1 + 0.12 * math.sin(t / 7) * math.sin(t / 13) + self.rng.gauss(0, 0.04)
        tws = max(0.0, w["tws"] * gustiness)
        twd = (w["twd"] + self.rng.gauss(0, 3)) % 360

        # Beat to windward on alternate tacks; head home when we stray too far.
        if int((t - self.t0) // self.tack_every) % 2:
            self.tack = -1
        else:
            self.tack = 1
        heading = (twd + 50 * self.tack) % 360
        if distance_nm(self.lat, self.lon, *self.home) > self.radius:
            heading = bearing_deg(self.lat, self.lon, *self.home)
        stw = self.stw * min(1.0, tws / 10) + self.rng.gauss(0, 0.1)
        stw = max(0.0, stw)
        d = stw * dt_s / 3600 / 60  # degrees of latitude
        self.lat += d * math.cos(math.radians(heading))
        self.lon += d * math.sin(math.radians(heading)) / max(0.1, math.cos(math.radians(self.lat)))

        # Apparent wind = true wind velocity - boat velocity.
        tx, ty = -tws * math.sin(math.radians(twd)), -tws * math.cos(math.radians(twd))
        bx, by = stw * math.sin(math.radians(heading)), stw * math.cos(math.radians(heading))
        ax, ay = tx - bx, ty - by
        aws = math.hypot(ax, ay)
        a_from = (math.degrees(math.atan2(-ax, -ay)) + 360) % 360
        awa = (a_from - heading) % 360

        g = time.gmtime(t)
        hms = time.strftime("%H%M%S", g) + ".00"
        dmy = time.strftime("%d%m%y", g)
        la, ns = _lat(self.lat)
        lo, ew = _lon(self.lon)
        p_bar = w["p"] / 1000
        return [
            with_checksum(f"GPRMC,{hms},A,{la},{ns},{lo},{ew},{stw:.1f},{heading:.1f},{dmy},,,A"),
            with_checksum(f"IIHDT,{heading:.1f},T"),
            with_checksum(f"IIVHW,{heading:.1f},T,,M,{stw:.2f},N,{stw * 1.852:.2f},K"),
            with_checksum(f"WIMWV,{awa:.1f},R,{aws:.1f},N,A"),
            with_checksum(f"WIMDA,{p_bar * 29.53:.3f},I,{p_bar:.5f},B,{w['air']:.1f},C,{w['water']:.1f},C,"
                          f"{w['rh']:.0f},,{w['dew']:.1f},C,,T,,M,,N,,M"),
        ]


async def serve_udp(target: str, lat: float, lon: float, rate_hz: float = 1.0) -> None:
    host, _, port = target.rpartition(":")
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=(host, int(port)),
                                                       allow_broadcast=True)
    sim = BoatSim(lat, lon)
    last = time.time()
    log.info("sending simulated NMEA to udp://%s", target)
    try:
        while True:
            await asyncio.sleep(1 / rate_hz)
            now = time.time()
            payload = "".join(s + "\r\n" for s in sim.step(now, now - last)).encode()
            transport.sendto(payload)
            last = now
    finally:
        transport.close()


async def serve_tcp(port: int, lat: float, lon: float, rate_hz: float = 1.0) -> None:
    clients: set[asyncio.StreamWriter] = set()

    async def on_client(reader, writer):
        clients.add(writer)
        try:
            await reader.read()  # wait until the client disconnects
        finally:
            clients.discard(writer)

    server = await asyncio.start_server(on_client, "0.0.0.0", port)
    sim = BoatSim(lat, lon)
    last = time.time()
    log.info("serving simulated NMEA on tcp://0.0.0.0:%d", port)
    async with server:
        while True:
            await asyncio.sleep(1 / rate_hz)
            now = time.time()
            payload = "".join(s + "\r\n" for s in sim.step(now, now - last)).encode()
            last = now
            for w in list(clients):
                try:
                    w.write(payload)
                    await w.drain()
                except ConnectionError:
                    clients.discard(w)
