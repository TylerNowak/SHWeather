"""Cross-platform behaviour: config lookup, paths, logging, Windows script checks."""

import logging
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from shweather.analysis import conditions, pressure
from shweather.config import (
    BoatProfile,
    NmeaSource,
    Settings,
    config_candidates,
    default_serial_device,
    load_settings,
    system_config_path,
)

ROOT = Path(__file__).resolve().parents[1]


def test_system_config_location_per_platform():
    assert system_config_path("linux") == Path("/etc/shweather/config.yaml")
    win = system_config_path("win32", {"PROGRAMDATA": r"C:\ProgramData"})
    assert win.name == "config.yaml" and "SHWeatherService" in str(win) and str(win).startswith(r"C:\ProgramData")
    cands = config_candidates(None, "win32", {"SHWEATHER_CONFIG": "D:/boat/cfg.yaml", "PROGRAMDATA": r"C:\ProgramData"})
    assert cands[0] == Path("D:/boat/cfg.yaml") and cands[-1] == win


def test_serial_default_follows_platform():
    assert default_serial_device("win32") == "COM3"
    assert default_serial_device("linux") == "/dev/ttyUSB0"
    assert NmeaSource(type="serial", device="COM7").device == "COM7"


def test_relative_paths_resolve_against_config_file(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "ProgramData" / "SHWeatherService"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "config.yaml"
    # BOM + relative paths, the way Notepad and install.ps1 write them
    cfg.write_text("\ufeffdata_dir: data\nlog_file: logs/shweather.log\nport: 8090\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # a service starts somewhere else entirely
    s = load_settings(cfg)
    assert s.port == 8090
    assert s.data_dir == cfg_dir.resolve() / "data"
    assert s.log_file == cfg_dir.resolve() / "logs" / "shweather.log"


def test_environment_variables_expand_in_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SHW_TEST_ROOT", str(tmp_path))
    s = Settings(data_dir="$SHW_TEST_ROOT/data", log_file="$SHW_TEST_ROOT/x.log")
    assert s.data_dir == tmp_path / "data" and s.log_file == tmp_path / "x.log"


def test_display_defaults():
    s = Settings()
    assert s.display.units == "auto" and s.display.nautical is True
    assert Settings(display={"units": "imperial", "nautical": False}).display.units == "imperial"
    with pytest.raises(ValueError):
        Settings(display={"units": "furlongs"})


def test_service_logging_goes_to_rotating_file_only(tmp_path, monkeypatch):
    from shweather.__main__ import setup_logging

    class NotATty:
        def isatty(self):
            return False

        def write(self, *_):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stderr", NotATty())
    setup_logging(Settings(log_file=tmp_path / "logs" / "shw.log"))
    handlers = logging.getLogger().handlers
    assert [type(h).__name__ for h in handlers] == ["RotatingFileHandler"]
    logging.getLogger("x").info("hello")
    assert "hello" in (tmp_path / "logs" / "shw.log").read_text()
    assert logging.getLogger("httpx").level == logging.WARNING
    for h in handlers:
        h.close()
    logging.getLogger().handlers.clear()


def test_structured_reasons_for_unit_aware_clients():
    boat = BoatProfile(max_wave_m=2.0, max_gust_kn=32)
    r = conditions.assess({"wind_speed_kn": 12, "wind_gust_kn": 36.4, "wave_height_m": 2.1}, boat)
    assert r["level"] == "nogo"
    assert [d["code"] for d in r["details"]] == ["gust_over_limit", "waves_over_limit"]
    assert r["details"][1] == {"level": "nogo", "code": "waves_over_limit", "wave_m": 2.1, "limit_m": 2.0}
    assert len(r["reasons"]) == len(r["details"])


def test_pressure_warnings_have_codes():
    w = pressure.warnings(-4.0, 985)
    assert [x["code"] for x in w] == ["falling_quickly", "deep_low"]
    assert w[1]["pressure_hpa"] == 985


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 (pwsh) not installed")
def test_windows_scripts(tmp_path):
    out = tmp_path / "config.yaml"
    res = subprocess.run(["pwsh", "-NoProfile", "-File", str(ROOT / "tests/windows/test_scripts.ps1")],
                         capture_output=True, text=True, env={**__import__("os").environ, "SHW_TEST_OUT": str(out)})
    assert res.returncode == 0, res.stdout + res.stderr
    s = load_settings(out)  # the config install.ps1 would write is valid for the server
    assert s.port == 8090 and s.log_file is not None and s.log_file.name == "shweather.log"


def test_web_assets_served_with_module_safe_types(tmp_path):
    """Windows registries often map .js to text/plain; browsers then refuse ES modules."""
    import mimetypes

    from fastapi.testclient import TestClient

    from shweather.app import create_app

    original = mimetypes.guess_type("x.js")[0]
    mimetypes.add_type("text/plain", ".js")  # what a hostile registry entry does
    try:
        with TestClient(create_app(Settings(data_dir=tmp_path, demo=True), start_background=False)) as c:
            assert c.get("/js/app.js").headers["content-type"].startswith("text/javascript")
            assert c.get("/js/units.js").headers["content-type"].startswith("text/javascript")
            assert c.get("/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
            assert c.get("/").headers["content-type"].startswith("text/html")
            # the app's code is revalidated on every load, so upgrades reach phones at once
            for path in ("/", "/js/app.js", "/js/radar.js", "/css/app.css", "/data/basemap.json", "/sw.js"):
                assert c.get(path).headers["cache-control"] == "no-cache", path
            etag = c.get("/js/radar.js").headers["etag"]
            assert c.get("/js/radar.js", headers={"If-None-Match": etag}).status_code == 304
    finally:
        mimetypes.add_type(original or "text/javascript", ".js")


def test_trace_log_level_does_not_crash():
    from shweather.__main__ import setup_logging

    setup_logging(Settings(log_level="trace"))
    assert logging.getLogger().level == 5
    logging.getLogger().handlers.clear()
    with pytest.raises(ValueError):
        Settings(log_level="verbose")


def _listener() -> socket.socket:
    """A socket listening on a free loopback port: "another program" holding the port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def test_port_held_by_another_program_is_explained(tmp_path):
    from shweather.__main__ import port_problem

    cfg = tmp_path / "config.yaml"
    with _listener() as other:
        port = other.getsockname()[1]
        msg = port_problem("127.0.0.1", port, cfg)
    assert msg.startswith(f"Port {port} is already in use by another program")
    assert str(cfg) in msg and "port:" in msg
    with _listener() as other:
        port = other.getsockname()[1]
        assert "--port" in port_problem("127.0.0.1", port, cfg, from_cli=True)


def test_free_port_is_not_a_problem():
    from shweather.__main__ import port_problem

    with _listener() as s:
        port = s.getsockname()[1]
    assert port_problem("127.0.0.1", port) is None
    assert port_problem("no-such-host.invalid", 8080) is None  # unclear cases say nothing


def test_serve_explains_a_busy_port_when_startup_fails(tmp_path, monkeypatch, caplog):
    import uvicorn

    from shweather import __main__ as cli

    with _listener() as other:
        port = other.getsockname()[1]
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"host: 127.0.0.1\nport: {port}\ndata_dir: data\n")
        monkeypatch.setattr(cli, "setup_logging", lambda settings: None)  # keep caplog's handler
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: sys.exit(3))  # as when the bind fails
        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            cli.main(["--config", str(cfg), "serve"])
    assert exc.value.code == 3
    assert f"Port {port} is already in use" in caplog.text
    assert str(cfg) in caplog.text


def test_linux_installer_copies_the_web_data_folder():
    """rsync excludes must be anchored: a bare "data" would also drop web/data (the offline map)."""
    text = (ROOT / "deploy/install.sh").read_text()
    line = next(x for x in text.splitlines() if x.startswith("rsync "))
    assert "--exclude data" not in line and "--exclude /data" in line
    assert (ROOT / "web/data/basemap.json").is_file()
