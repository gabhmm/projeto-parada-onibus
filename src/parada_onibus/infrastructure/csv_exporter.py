"""Exportador CSV em streaming para análise de dados das paradas de ônibus."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator

from parada_onibus.domain.models import StopReading

CSV_HEADERS: tuple[str, ...] = (
    "timestamp",
    "parada_id",
    "nome",
    "origem",
    "temperatura",
    "limite",
    "ventilacao",
    "modo",
)


def iter_csv_chunks(readings: Iterable[StopReading]) -> Iterator[str]:
    """Gera linhas CSV em streaming (cabeçalho + leituras) sem acumular dados em RAM."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    writer.writerow(CSV_HEADERS)
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    for reading in readings:
        writer.writerow(
            [
                reading.timestamp.isoformat(),
                reading.stop_id,
                reading.stop_name,
                reading.origin.value,
                f"{reading.temperature:.2f}",
                f"{reading.temp_limit:.2f}",
                "true" if reading.ventilation else "false",
                reading.mode.value,
            ]
        )
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
