"""Modelos de domínio tipados com dataclasses imutáveis e StrEnums (Python 3.12+)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class StopOrigin(StrEnum):
    """Origem física ou simulada de uma parada de ônibus."""

    REAL = "REAL"
    SIMULADA = "SIMULADA"


class ControlMode(StrEnum):
    """Modo de operação do controle de ventilação."""

    AUTO = "AUTO"
    MANUAL = "MANUAL"


class StopStatus(StrEnum):
    """Estado visual e operacional atual de uma parada."""

    NORMAL = "NORMAL"
    VENTILANDO = "VENTILANDO"
    DESCONECTADO = "DESCONECTADO"


class CommandType(StrEnum):
    """Tipos de comando suportados no contrato Serial JSON."""

    SET_LIMIT = "set_limit"
    SET_FAN = "set_fan"
    SET_MODE = "set_mode"


class EventType(StrEnum):
    """Categorias de eventos persistidos no histórico."""

    VENTILATION_ON = "VENTILATION_ON"
    VENTILATION_OFF = "VENTILATION_OFF"
    COMMAND_SENT = "COMMAND_SENT"
    LIMIT_CHANGED = "LIMIT_CHANGED"
    SERIAL_CONNECTED = "SERIAL_CONNECTED"
    SERIAL_DISCONNECTED = "SERIAL_DISCONNECTED"


@dataclass(frozen=True, slots=True)
class StopConfig:
    """Configuração estática de uma parada monitorada."""

    stop_id: str
    name: str
    origin: StopOrigin
    derivation_factor: float
    initial_limit: float = 30.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Configuração global da aplicação carregada de TOML e variáveis de ambiente."""

    host: str = "0.0.0.0"
    port: int = 5000
    mode: str = "serial"
    serial_port: str = "/dev/ttyACM0"
    baud_rate: int = 9600
    serial_timeout_seconds: float = 6.0
    serial_reconnect_max_seconds: float = 5.0
    db_path: str = "data/parada_onibus.db"
    db_persist_interval_seconds: float = 1.0
    chart_history_points: int = 120
    simulator_interval_seconds: float = 1.0
    stops: tuple[StopConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class ArduinoTelemetry:
    """Telemetria validada recebida do Arduino (Parada 1) via Serial NDJSON."""

    temp: float
    fan: bool
    limit: float | None = None
    mode: ControlMode | None = None
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ArduinoAck:
    """Confirmação opcional enviada pelo Arduino em resposta a um comando."""

    cmd: str
    ok: bool = True
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ArduinoCommand:
    """Comando estruturado a ser enviado em NDJSON para o Arduino."""

    cmd: CommandType
    value: float | bool | str


@dataclass(frozen=True, slots=True)
class StoredStopSetting:
    """Configuração operacional persistida no SQLite para sobreviver a reinicializações."""

    stop_id: str
    temp_limit: float
    mode: ControlMode
    manual_fan: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StopReading:
    """Leitura individual de uma parada em um instante específico."""

    timestamp: datetime
    stop_id: str
    stop_name: str
    origin: StopOrigin
    temperature: float
    temp_limit: float
    ventilation: bool
    mode: ControlMode

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "stop_id": self.stop_id,
            "stop_name": self.stop_name,
            "origin": self.origin.value,
            "temperature": self.temperature,
            "temp_limit": self.temp_limit,
            "ventilation": self.ventilation,
            "mode": self.mode.value,
        }


@dataclass(frozen=True, slots=True)
class StopState:
    """Snapshot do estado atual de uma parada exibido na dashboard."""

    stop_id: str
    name: str
    origin: StopOrigin
    derivation_factor: float
    temperature: float | None
    temp_limit: float
    ventilation: bool
    mode: ControlMode
    status: StopStatus
    last_updated: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_id": self.stop_id,
            "name": self.name,
            "origin": self.origin.value,
            "derivation_factor": self.derivation_factor,
            "temperature": self.temperature,
            "temp_limit": self.temp_limit,
            "ventilation": self.ventilation,
            "mode": self.mode.value,
            "status": self.status.value,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }


@dataclass(frozen=True, slots=True)
class SystemEvent:
    """Evento operacional registrado para auditoria e feed na dashboard."""

    timestamp: datetime
    stop_id: str
    event_type: EventType
    message: str
    payload_json: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "stop_id": self.stop_id,
            "event_type": self.event_type.value,
            "message": self.message,
            "payload_json": self.payload_json,
        }


@dataclass(frozen=True, slots=True)
class NetworkSummary:
    """Resumo consolidado da rede de paradas monitoradas."""

    connected: bool
    operation_mode: str
    average_temperature: float | None
    max_temperature: float | None
    ventilating_count: int
    total_stops: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "operation_mode": self.operation_mode,
            "average_temperature": self.average_temperature,
            "max_temperature": self.max_temperature,
            "ventilating_count": self.ventilating_count,
            "total_stops": self.total_stops,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Resultado de um comando de controle acionado pelo usuário."""

    ok: bool
    stop_id: str
    message: str
    state: StopState | None = None
    http_status: int = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stop_id": self.stop_id,
            "message": self.message,
            "state": self.state.to_dict() if self.state else None,
        }
