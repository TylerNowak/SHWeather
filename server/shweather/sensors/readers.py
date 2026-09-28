"""Async NMEA 0183 inputs: TCP client, UDP listener, serial port, file replay.

Each reader runs forever, reconnecting with backoff, and feeds lines into the SensorHub.
Typical sources: a Wi-Fi/Ethernet multiplexer (TCP 10110 or UDP broadcast), OpenCPN or
Signal K re-broadcasting NMEA 0183, or a USB-RS422 adapter wired to the instrument bus.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator

from ..config import NmeaSource
from .hub import SensorHub

log = logging.getLogger(__name__)


def source_name(cfg: NmeaSource) -> str:
    if cfg.type == "tcp":
        return f"nmea-tcp://{cfg.host or '127.0.0.1'}:{cfg.port}"
    if cfg.type == "udp":
        return f"nmea-udp://{cfg.host or '0.0.0.0'}:{cfg.port}"
    if cfg.type == "serial":
        return f"nmea-serial:{cfg.device}"
    return f"nmea-file:{cfg.path}"


async def _tcp_lines(cfg: NmeaSource) -> AsyncIterator[str]:
    reader, writer = await asyncio.open_connection(cfg.host or "127.0.0.1", cfg.port)
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=60)
            if not line:
                raise ConnectionError("connection closed")
            yield line.decode("ascii", "ignore")
    finally:
        writer.close()


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    def datagram_received(self, data: bytes, addr) -> None:
        for line in data.decode("ascii", "ignore").splitlines():
            self.queue.put_nowait(line)


async def _udp_lines(cfg: NmeaSource) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=10000)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _UdpProtocol(queue), local_addr=(cfg.host or "0.0.0.0", cfg.port), allow_broadcast=True)
    try:
        while True:
            yield await queue.get()
    finally:
        transport.close()


async def _serial_lines(cfg: NmeaSource) -> AsyncIterator[str]:
    try:
        import serial  # type: ignore
    except ImportError as e:
        raise RuntimeError("serial input needs pyserial: pip install 'shweather[serial]'") from e
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | Exception] = asyncio.Queue(maxsize=10000)
    stop = threading.Event()

    def worker():
        try:
            with serial.Serial(cfg.device, cfg.baudrate, timeout=2) as ser:
                while not stop.is_set():
                    raw = ser.readline()
                    if raw:
                        loop.call_soon_threadsafe(queue.put_nowait, raw.decode("ascii", "ignore"))
        except Exception as exc:  # surface to the async side
            loop.call_soon_threadsafe(queue.put_nowait, exc)

    t = threading.Thread(target=worker, name=f"serial-{cfg.device}", daemon=True)
    t.start()
    try:
        while True:
            item = await queue.get()
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop.set()


async def _file_lines(cfg: NmeaSource) -> AsyncIterator[str]:
    if not cfg.path:
        raise ValueError("file source needs 'path'")
    delay = 1.0 / cfg.rate_hz if cfg.rate_hz > 0 else 0
    while True:
        with open(cfg.path, encoding="ascii", errors="ignore") as fh:
            for line in fh:
                yield line
                if delay:
                    await asyncio.sleep(delay)
        if not cfg.loop:
            return


_LINES = {"tcp": _tcp_lines, "udp": _udp_lines, "serial": _serial_lines, "file": _file_lines}


async def run_nmea_source(cfg: NmeaSource, hub: SensorHub) -> None:
    name = source_name(cfg)
    backoff = 2.0
    while True:
        try:
            log.info("NMEA source %s starting", name)
            async for line in _LINES[cfg.type](cfg):
                hub.feed_nmea(line, name)
                backoff = 2.0
            if cfg.type == "file":
                log.info("NMEA file %s finished", cfg.path)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            hub.mark_source(name, connected=False, error=str(exc))
            log.warning("NMEA source %s: %s (retry in %.0fs)", name, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
