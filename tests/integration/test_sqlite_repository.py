"""Testes de integração do repositório SQLite (WAL, reinicialização e CSV)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parada_onibus.domain.models import (
    ControlMode,
    EventType,
    StopOrigin,
    StopReading,
    SystemEvent,
)
from parada_onibus.infrastructure.csv_exporter import CSV_HEADERS, iter_csv_chunks
from parada_onibus.infrastructure.sqlite_repository import SQLiteRepository


def test_sqlite_persists_readings_events_and_settings_across_restarts(tmp_path: Path) -> None:
    db_file = str(tmp_path / "paradas.db")
    t0 = datetime(2026, 11, 22, 18, 0, 0, tzinfo=UTC)

    repo = SQLiteRepository(db_file)
    repo.initialize()
    repo.save_readings(
        [
            StopReading(
                t0, "parada-1", "Parada 1", StopOrigin.REAL, 30.5, 30.0, True, ControlMode.AUTO
            ),
            StopReading(
                t0, "parada-2", "Parada 2", StopOrigin.SIMULADA, 32.03, 30.0, True, ControlMode.AUTO
            ),
            StopReading(
                t0 + timedelta(seconds=1),
                "parada-1",
                "Parada 1",
                StopOrigin.REAL,
                29.5,
                30.0,
                False,
                ControlMode.AUTO,
            ),
        ]
    )
    repo.save_event(SystemEvent(t0, "parada-1", EventType.VENTILATION_ON, "Ligou cooler"))
    repo.save_stop_setting("parada-2", 28.5, ControlMode.MANUAL, False)
    repo.close()

    # Reabre o banco simulando reinicialização da Raspberry Pi
    repo2 = SQLiteRepository(db_file)
    repo2.initialize()
    try:
        p1_readings = repo2.get_recent_readings("parada-1", limit=10)
        assert [r.temperature for r in p1_readings] == [30.5, 29.5]

        events = repo2.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0].event_type == EventType.VENTILATION_ON

        settings = repo2.load_stop_settings()
        assert "parada-2" in settings
        assert settings["parada-2"].temp_limit == 28.5
        assert settings["parada-2"].mode == ControlMode.MANUAL

        # Valida exportação CSV geral e filtrada
        all_csv = "".join(iter_csv_chunks(repo2.iter_readings_for_export()))
        rows = list(csv.reader(io.StringIO(all_csv)))
        assert tuple(rows[0]) == CSV_HEADERS
        assert len(rows) == 4  # cabeçalho + 3 leituras

        p2_csv = "".join(iter_csv_chunks(repo2.iter_readings_for_export(stop_id="parada-2")))
        p2_rows = list(csv.reader(io.StringIO(p2_csv)))
        assert len(p2_rows) == 2
        assert p2_rows[1][1] == "parada-2"
        assert p2_rows[1][4] == "32.03"
        assert p2_rows[1][6] == "true"
    finally:
        repo2.close()
