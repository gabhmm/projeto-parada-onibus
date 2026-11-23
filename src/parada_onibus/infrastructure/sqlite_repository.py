"""Repositório SQLite otimizado para Raspberry Pi (WAL mode e gravação em lote)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from parada_onibus.domain.models import (
    ControlMode,
    EventType,
    StopOrigin,
    StopReading,
    StoredStopSetting,
    SystemEvent,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    stop_id     TEXT    NOT NULL,
    stop_name   TEXT    NOT NULL,
    origin      TEXT    NOT NULL,
    temperature REAL    NOT NULL,
    temp_limit  REAL    NOT NULL,
    ventilation INTEGER NOT NULL,
    mode        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_stop_ts ON readings (stop_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_readings_ts      ON readings (timestamp DESC);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    stop_id      TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    message      TEXT NOT NULL,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events (id DESC);

CREATE TABLE IF NOT EXISTS stop_settings (
    stop_id    TEXT PRIMARY KEY,
    temp_limit REAL    NOT NULL,
    mode       TEXT    NOT NULL,
    manual_fan INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL
);
"""


class SQLiteRepository:
    """Persistência thread-safe em SQLite para leituras, eventos e configurações das paradas."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            if self._db_path != ":memory:":
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def save_readings(self, readings: Sequence[StopReading]) -> None:
        if not readings:
            return
        rows = [
            (
                r.timestamp.isoformat(),
                r.stop_id,
                r.stop_name,
                r.origin.value,
                r.temperature,
                r.temp_limit,
                1 if r.ventilation else 0,
                r.mode.value,
            )
            for r in readings
        ]
        with self._lock:
            conn = self._require_conn()
            with conn:
                conn.executemany(
                    """
                    INSERT INTO readings (
                        timestamp, stop_id, stop_name, origin,
                        temperature, temp_limit, ventilation, mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def save_event(self, event: SystemEvent) -> None:
        with self._lock:
            conn = self._require_conn()
            with conn:
                conn.execute(
                    """
                    INSERT INTO events (timestamp, stop_id, event_type, message, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.timestamp.isoformat(),
                        event.stop_id,
                        event.event_type.value,
                        event.message,
                        event.payload_json,
                    ),
                )

    def save_stop_setting(
        self,
        stop_id: str,
        temp_limit: float,
        mode: ControlMode,
        manual_fan: bool,
    ) -> None:
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._require_conn()
            with conn:
                conn.execute(
                    """
                    INSERT INTO stop_settings (stop_id, temp_limit, mode, manual_fan, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(stop_id) DO UPDATE SET
                        temp_limit = excluded.temp_limit,
                        mode = excluded.mode,
                        manual_fan = excluded.manual_fan,
                        updated_at = excluded.updated_at
                    """,
                    (stop_id, temp_limit, mode.value, 1 if manual_fan else 0, now_iso),
                )

    def load_stop_settings(self) -> dict[str, StoredStopSetting]:
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                "SELECT stop_id, temp_limit, mode, manual_fan, updated_at FROM stop_settings"
            ).fetchall()
        result: dict[str, StoredStopSetting] = {}
        for row in rows:
            setting = StoredStopSetting(
                stop_id=str(row["stop_id"]),
                temp_limit=float(row["temp_limit"]),
                mode=ControlMode(str(row["mode"])),
                manual_fan=bool(row["manual_fan"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            result[setting.stop_id] = setting
        return result

    def get_recent_readings(self, stop_id: str, limit: int = 120) -> list[StopReading]:
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                """
                SELECT timestamp, stop_id, stop_name, origin,
                       temperature, temp_limit, ventilation, mode
                FROM readings
                WHERE stop_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (stop_id, limit),
            ).fetchall()
        rows.reverse()
        return [self._row_to_reading(row) for row in rows]

    def get_recent_events(self, limit: int = 50) -> list[SystemEvent]:
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                """
                SELECT timestamp, stop_id, event_type, message, payload_json
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            SystemEvent(
                timestamp=datetime.fromisoformat(str(row["timestamp"])),
                stop_id=str(row["stop_id"]),
                event_type=EventType(str(row["event_type"])),
                message=str(row["message"]),
                payload_json=str(row["payload_json"]) if row["payload_json"] is not None else None,
            )
            for row in rows
        ]

    def iter_readings_for_export(self, stop_id: str | None = None) -> Iterator[StopReading]:
        """Itera leituras em ordem cronológica em lotes para streaming CSV com baixa RAM."""
        last_id = 0
        batch_size = 500
        while True:
            with self._lock:
                conn = self._require_conn()
                if stop_id is not None:
                    rows = conn.execute(
                        """
                        SELECT id, timestamp, stop_id, stop_name, origin,
                               temperature, temp_limit, ventilation, mode
                        FROM readings
                        WHERE stop_id = ? AND id > ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (stop_id, last_id, batch_size),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, timestamp, stop_id, stop_name, origin,
                               temperature, temp_limit, ventilation, mode
                        FROM readings
                        WHERE id > ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (last_id, batch_size),
                    ).fetchall()

            if not rows:
                break
            for row in rows:
                last_id = int(row["id"])
                yield self._row_to_reading(row)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteRepository não foi inicializado. Chame initialize() antes.")
        return self._conn

    @staticmethod
    def _row_to_reading(row: sqlite3.Row) -> StopReading:
        return StopReading(
            timestamp=datetime.fromisoformat(str(row["timestamp"])),
            stop_id=str(row["stop_id"]),
            stop_name=str(row["stop_name"]),
            origin=StopOrigin(str(row["origin"])),
            temperature=float(row["temperature"]),
            temp_limit=float(row["temp_limit"]),
            ventilation=bool(row["ventilation"]),
            mode=ControlMode(str(row["mode"])),
        )
