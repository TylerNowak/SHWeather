"""Command line: ``shweather serve | fetch | simulate | config``."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import errno
import json
import logging
import os
import socket
import sys

from .config import Settings, find_config_file, load_settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Socket errors as Python reports them on Linux/macOS and on Windows (WSA codes).
PORT_IN_USE = {errno.EADDRINUSE, 10048}
PORT_DENIED = {errno.EACCES, 10013}   # Windows: a reserved (excluded) port or security software
SPARE_PORTS = (8090, 8081, 8088, 8000, 8888, 9080)


def _bind_error(host: str | None, port: int) -> OSError | None:
    """Try to listen on host:port the way the server (asyncio) does: the error, or None if it works."""
    try:
        infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError:
        return None
    for family, kind, proto, _, addr in infos:
        try:
            with socket.socket(family, kind, proto) as s:
                if os.name == "posix":
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                s.bind(addr)
        except OSError as exc:
            return exc
    return None


def port_problem(host: str | None, port: int, config: os.PathLike | str | None = None,
                 from_cli: bool = False) -> str | None:
    """Why the server can't listen on host:port, in plain words; None if the port looks fine."""
    exc = _bind_error(host, port)
    if exc is None or exc.errno not in PORT_IN_USE | PORT_DENIED:
        return None
    spare = next((p for p in SPARE_PORTS if p != port and _bind_error(host, p) is None), None)
    other = f"a free port such as {spare}" if spare else "a free port"
    if from_cli:
        fix = f"start it with --port set to {other}"
    else:
        fix = f"set 'port:' in {config or 'config.yaml'} to {other} and restart"
        if sys.platform == "win32":
            fix += f" (re-running install.ps1 -Port {spare or 8090} does that and updates the firewall rule)"
    if exc.errno in PORT_IN_USE:
        return f"Port {port} is already in use by another program, so the server cannot start. Stop that program, or {fix}."
    if sys.platform == "win32":
        return (f"Windows does not allow port {port}: it is in a reserved range (Hyper-V, WSL and Docker reserve "
                f"blocks of ports; list them with 'netsh interface ipv4 show excludedportrange protocol=tcp') "
                f"or security software blocks it. To use another port, {fix}.")
    if port < 1024:
        return f"Port {port} needs root privileges. To use a port above 1023 instead, {fix}."
    return f"Not allowed to listen on port {port}. To use another port, {fix}."


def setup_logging(settings: Settings) -> None:
    """Console logging, plus a rotating file when log_file is set (Windows services have no journal)."""
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = []
    interactive = bool(sys.stderr and sys.stderr.isatty())
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(settings.log_file, maxBytes=5_000_000, backupCount=3,
                                            encoding="utf-8"))
    if interactive or not settings.log_file:
        # Running as a service with stderr redirected to a file: the rotated log file is
        # enough; duplicating into an ever-growing stderr file is not.
        handlers.append(logging.StreamHandler())
    logging.addLevelName(5, "TRACE")  # uvicorn's extra level; register it before we use it
    level = 5 if settings.log_level == "trace" else settings.log_level.upper()
    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers, force=True)
    if settings.log_level not in ("debug", "trace"):
        logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too chatty


def _settings(args):
    settings = load_settings(args.config)
    if getattr(args, "demo", False):
        settings.demo = True
    if getattr(args, "host", None):
        settings.host = args.host
    if getattr(args, "port", None):
        settings.port = args.port
    return settings


def cmd_serve(args) -> int:
    import uvicorn

    from .app import create_app

    settings = _settings(args)
    setup_logging(settings)
    try:
        # log_config=None: uvicorn's loggers propagate to our handlers (console + optional file).
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level,
                    log_config=None, access_log=settings.access_log, proxy_headers=True)
    except SystemExit as exc:
        # uvicorn exits when it can't start; the usual reason is the port, and its own
        # message ("[Errno 10048] ... only one usage of each socket address") says little.
        if exc.code:
            hint = port_problem(settings.host, settings.port, find_config_file(args.config),
                                from_cli=bool(getattr(args, "port", None)))
            if hint:
                logging.getLogger("shweather").error(hint)
        raise
    return 0


def cmd_fetch(args) -> int:
    from .app import build_service

    settings = _settings(args)
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")

    async def run():
        svc = build_service(settings)
        try:
            if args.lat is not None and args.lon is not None:
                svc.set_position(args.lat, args.lon, "cli")
            result = await svc.refresh_all(args.radius_nm)
            print(json.dumps(result, indent=2, default=str))
            pos = svc.position()
            fc = svc.forecast_view(pos["lat"], pos["lon"], hours=6, past_hours=0)
            if fc:
                s = fc["series"]
                print("\nNext hours at", f"{pos['lat']:.3f},{pos['lon']:.3f}")
                for i, t in enumerate(s["time"]):
                    when = dt.datetime.fromtimestamp(t, dt.UTC).strftime("%a %H:%MZ")
                    print(f"  {when}: wind {s['wind_speed_kn'][i]} kn from {s['wind_dir_deg'][i]}, "
                          f"gust {s['wind_gust_kn'][i]}, waves {s.get('wave_height_m', [None] * 99)[i]} m, "
                          f"{fc['assessment'][i]['level']}")
        finally:
            await svc.aclose()

    asyncio.run(run())
    return 0


def cmd_simulate(args) -> int:
    from .simulator import serve_tcp, serve_udp

    logging.basicConfig(level="INFO", format="%(asctime)s %(message)s")
    try:
        if args.tcp:
            asyncio.run(serve_tcp(args.tcp, args.lat, args.lon, args.rate))
        else:
            asyncio.run(serve_udp(args.udp, args.lat, args.lon, args.rate))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_config(args) -> int:
    settings = _settings(args)
    print(json.dumps(settings.model_dump(mode="json"), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shweather", description="Self-hosted marine weather for your boat")
    p.add_argument("--config", "-c", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app, API, sensors and scheduler")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--demo", action="store_true", help="synthetic data, no network or instruments needed")
    s.set_defaults(func=cmd_serve)

    f = sub.add_parser("fetch", help="download forecasts once and print a summary")
    f.add_argument("--lat", type=float)
    f.add_argument("--lon", type=float)
    f.add_argument("--radius-nm", type=float, help="passage download radius")
    f.add_argument("--demo", action="store_true")
    f.set_defaults(func=cmd_fetch)

    m = sub.add_parser("simulate", help="emit simulated NMEA 0183 over UDP or TCP")
    g = m.add_mutually_exclusive_group()
    g.add_argument("--udp", default="127.0.0.1:10110", help="HOST:PORT to send to (default %(default)s)")
    g.add_argument("--tcp", type=int, help="serve on this TCP port instead")
    m.add_argument("--lat", type=float, default=41.90)
    m.add_argument("--lon", type=float, default=-87.50)
    m.add_argument("--rate", type=float, default=1.0, help="sentence bursts per second")
    m.set_defaults(func=cmd_simulate)

    c = sub.add_parser("config", help="print the effective configuration")
    c.set_defaults(func=cmd_config)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
