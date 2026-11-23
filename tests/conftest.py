"""Fixtures compartilhadas para testes unitários e de integração."""

from __future__ import annotations

from pathlib import Path

import pytest

from parada_onibus.domain.models import AppConfig, ArduinoCommand
from parada_onibus.domain.ports import LineCallback, StatusCallback
from parada_onibus.infrastructure.config import default_stops


class FakeTransport:
    """Transporte determinístico em memória para testar Serial, desconexões e comandos."""

    def __init__(self, *, initially_connected: bool = True, mode: str = "serial") -> None:
        self._connected = initially_connected
        self._mode = mode
        self._on_line: LineCallback | None = None
        self._on_status: StatusCallback | None = None
        self.sent_commands: list[ArduinoCommand] = []
        self.started = False
        self.stopped = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode_name(self) -> str:
        return self._mode

    def start(self, on_line: LineCallback, on_status: StatusCallback) -> None:
        self._on_line = on_line
        self._on_status = on_status
        self.started = True
        on_status(self._connected, "FakeTransport iniciado")

    def stop(self) -> None:
        self.stopped = True
        self._connected = False

    def send_command(self, command: ArduinoCommand) -> bool:
        if not self._connected:
            return False
        self.sent_commands.append(command)
        return True

    def emit_line(self, line: str) -> None:
        assert self._on_line is not None
        self._on_line(line)

    def set_connected(self, connected: bool, reason: str = "status alterado") -> None:
        self._connected = connected
        if self._on_status is not None:
            self._on_status(connected, reason)


@pytest.fixture
def test_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        host="127.0.0.1",
        port=5000,
        mode="serial",
        serial_port="/dev/null",
        baud_rate=9600,
        serial_timeout_seconds=5.0,
        db_path=str(tmp_path / "test_parada.db"),
        db_persist_interval_seconds=0.0,
        chart_history_points=60,
        simulator_interval_seconds=0.05,
        stops=default_stops(),
    )
