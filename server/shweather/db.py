"""SQLite storage: HTTP response cache, forecast grids, onboard observations, positions.

A single connection guarded by a lock is plenty for a Pi: requests are small, and writes
from the sensor hub are batched once a minute to spare the SD card.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    updated_at REAL NOT NULL,
    value      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at  REAL NOT NULL,
    kind        TEXT NOT NULL,          -- 'auto' | 'passage'
    center_lat  REAL NOT NULL,
    center_lon  REAL NOT NULL,
    min_lat     REAL NOT NULL,
    max_lat     REAL NOT NULL,
    min_lon     REAL NOT NULL,
    max_lon     REAL NOT NULL,
    spacing_deg REAL NOT NULL,
    model       TEXT,
    first_time  INTEGER,
    last_time   INTEGER,
    sources     TEXT                    -- JSON list of sources that contributed
);

CREATE TABLE IF NOT EXISTS forecast_points (
    run_id  INTEGER NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
    lat     REAL NOT NULL,
    lon     REAL NOT NULL,
    series  TEXT NOT NULL               -- JSON {time: [...], field: [...], ...}
);
CREATE INDEX IF NOT EXISTS forecast_points_run ON forecast_points(run_id);

CREATE TABLE IF NOT EXISTS observations (
    ts     REAL NOT NULL,
    metric TEXT NOT NULL,
    value  REAL NOT NULL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS observations_metric_ts ON observations(metric, ts);

CREATE TABLE IF NOT EXISTS positions (
    ts     REAL NOT NULL,
    lat    REAL NOT NULL,
    lon    REAL NOT NULL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS positions_ts ON positions(ts);

CREATE TABLE IF NOT EXISTS net_usage (
    day   TEXT PRIMARY KEY,
    bytes INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- key/value (cached API payloads, runtime settings, source status) ----

    def kv_set(self, key: str, value: Any, ts: float | None = None) -> None:
        self.execute("INSERT INTO kv(key, updated_at, value) VALUES (?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at, value=excluded.value",
                     (key, ts or time.time(), json.dumps(value)))

    def kv_get(self, key: str, max_age_s: float | None = None) -> tuple[Any, float] | None:
        rows = self.query("SELECT value, updated_at FROM kv WHERE key=?", (key,))
        if not rows:
            return None
        updated = rows[0]["updated_at"]
        if max_age_s is not None and time.time() - updated > max_age_s:
            return None
        return json.loads(rows[0]["value"]), updated

    # ---- forecast grids ----

    def save_run(self, *, kind: str, center: tuple[float, float], bbox, spacing_deg: float,
                 model: str, points: Iterable[tuple[float, float, dict]], sources: list[str],
                 fetched_at: float | None = None) -> int:
        pts = list(points)
        times = [t for _, _, s in pts for t in (s.get("time") or [])]
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO forecast_runs(fetched_at, kind, center_lat, center_lon, min_lat, max_lat,"
                    " min_lon, max_lon, spacing_deg, model, first_time, last_time, sources)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fetched_at or time.time(), kind, center[0], center[1], bbox.min_lat, bbox.max_lat,
                     bbox.min_lon, bbox.max_lon, spacing_deg, model,
                     min(times) if times else None, max(times) if times else None, json.dumps(sources)))
                run_id = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO forecast_points(run_id, lat, lon, series) VALUES (?,?,?,?)",
                    [(run_id, la, lo, json.dumps(s, separators=(",", ":"))) for la, lo, s in pts])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return run_id

    def runs(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.query(
            "SELECT * FROM forecast_runs ORDER BY fetched_at DESC LIMIT ?", (limit,))]

    def run_points(self, run_id: int) -> list[tuple[float, float, dict]]:
        return [(r["lat"], r["lon"], json.loads(r["series"])) for r in self.query(
            "SELECT lat, lon, series FROM forecast_points WHERE run_id=?", (run_id,))]

    def prune_runs(self, keep_auto: int = 6, now: float | None = None) -> None:
        now = now or time.time()
        auto_ids = [r["id"] for r in self.query(
            "SELECT id FROM forecast_runs WHERE kind='auto' ORDER BY fetched_at DESC")]
        stale = auto_ids[keep_auto:]
        stale += [r["id"] for r in self.query(
            "SELECT id FROM forecast_runs WHERE kind='passage' AND last_time < ?", (now,))]
        for rid in stale:
            self.execute("DELETE FROM forecast_runs WHERE id=?", (rid,))

    # ---- observations ----

    def add_observations(self, rows: Iterable[tuple[float, str, float, str | None]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            self._conn.executemany("INSERT INTO observations(ts, metric, value, source) VALUES (?,?,?,?)", rows)

    def observations(self, metric: str, since: float, until: float | None = None) -> list[tuple[float, float]]:
        until = until or time.time() + 1
        return [(r["ts"], r["value"]) for r in self.query(
            "SELECT ts, value FROM observations WHERE metric=? AND ts>=? AND ts<=? ORDER BY ts",
            (metric, since, until))]

    def prune_observations(self, older_than: float) -> None:
        self.execute("DELETE FROM observations WHERE ts < ?", (older_than,))
        self.execute("DELETE FROM positions WHERE ts < ?", (older_than,))

    # ---- positions ----

    def add_position(self, ts: float, lat: float, lon: float, source: str) -> None:
        self.execute("INSERT INTO positions(ts, lat, lon, source) VALUES (?,?,?,?)", (ts, lat, lon, source))

    def last_position(self) -> dict | None:
        rows = self.query("SELECT ts, lat, lon, source FROM positions ORDER BY ts DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    # ---- network usage accounting ----

    def add_net_bytes(self, n: int, day: str) -> None:
        self.execute("INSERT INTO net_usage(day, bytes) VALUES (?, ?) "
                     "ON CONFLICT(day) DO UPDATE SET bytes = bytes + excluded.bytes", (day, n))

    def net_bytes(self, day: str) -> int:
        rows = self.query("SELECT bytes FROM net_usage WHERE day=?", (day,))
        return rows[0]["bytes"] if rows else 0

    def net_bytes_month(self, month: str) -> int:
        """Total for a 'YYYY-MM' month."""
        rows = self.query("SELECT COALESCE(SUM(bytes), 0) AS b FROM net_usage WHERE day LIKE ?", (month + "-%",))
        return int(rows[0]["b"])

    def net_history(self, days: int = 31) -> list[dict]:
        return [dict(r) for r in self.query("SELECT day, bytes FROM net_usage ORDER BY day DESC LIMIT ?", (days,))]
