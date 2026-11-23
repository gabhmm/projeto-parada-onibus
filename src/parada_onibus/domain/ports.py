"""Contratos (Protocols) para inversão de dependência entre domínio e infraestrutura."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Protocol

from parada_onibus.domain.models import (
    ArduinoCommand,
    ControlMode,
    StopReading,
    StoredStopSetting,
    SystemEvent,
)

LineCallback = Callable[[str], None]
StatusCallback = Callable[[bool, str], None]


class TelemetryTransport(Protocol):
    """Interface de transporte bidirecional (Serial real ou Simulador)."""

    @property
    def is_connected(self) -> bool: ...

    @property
    def mode_name(self) -> str: ...

    def start(self, on_line: LineCallback, on_status: StatusCallback) -> None: ...

    def stop(self) -> None: ...

    def send_command(self, command: ArduinoCommand) -> bool: ...


class DataRepository(Protocol):
    """Interface de persistência de leituras, eventos e configurações por parada."""

    def initialize(self) -> None: ...

    def close(self) -> None: ...

    def save_readings(self, readings: Sequence[StopReading]) -> None: ...

    def save_event(self, event: SystemEvent) -> None: ...

    def save_stop_setting(
        self,
        stop_id: str,
        temp_limit: float,
        mode: ControlMode,
        manual_fan: bool,
    ) -> None: ...

    def load_stop_settings(self) -> dict[str, StoredStopSetting]: ...

    def get_recent_readings(self, stop_id: str, limit: int = 120) -> list[StopReading]: ...

    def get_recent_events(self, limit: int = 50) -> list[SystemEvent]: ...

    def iter_readings_for_export(self, stop_id: str | None = None) -> Iterator[StopReading]: ...
