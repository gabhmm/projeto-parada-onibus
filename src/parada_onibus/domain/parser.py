"""Parser defensivo de NDJSON da porta Serial e serializador de comandos."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from parada_onibus.domain.models import (
    ArduinoAck,
    ArduinoCommand,
    ArduinoTelemetry,
    ControlMode,
)

MIN_VALID_TEMP = -20.0
MAX_VALID_TEMP = 100.0
MIN_LIMIT_TEMP = 15.0
MAX_LIMIT_TEMP = 50.0


class ParseError(ValueError):
    """Erro disparado quando uma linha recebida da Serial não atende ao contrato NDJSON."""


def validate_temperature_limit(limit: float) -> float:
    """Valida e normaliza uma temperatura limite configurada pelo usuário."""
    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        raise ValueError("O limite de temperatura deve ser numérico.")
    numeric = float(limit)
    if not math.isfinite(numeric):
        raise ValueError("O limite de temperatura deve ser um número finito.")
    if not (MIN_LIMIT_TEMP <= numeric <= MAX_LIMIT_TEMP):
        raise ValueError(
            f"O limite de temperatura deve estar entre {MIN_LIMIT_TEMP:.1f} °C "
            f"e {MAX_LIMIT_TEMP:.1f} °C."
        )
    return round(numeric, 2)


def _extract_finite_float(payload: dict[str, Any], key: str) -> float:
    raw = payload.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ParseError(f"Campo '{key}' deve ser numérico.")
    value = float(raw)
    if not math.isfinite(value):
        raise ParseError(f"Campo '{key}' contém valor não finito.")
    return value


def parse_serial_line(
    raw_line: str,
    *,
    received_at: datetime | None = None,
) -> ArduinoTelemetry | ArduinoAck:
    """Faz o parsing defensivo de uma linha NDJSON vinda do Arduino."""
    cleaned = raw_line.strip()
    if not cleaned:
        raise ParseError("Linha serial vazia.")

    try:
        decoded: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ParseError(f"JSON malformado: {exc.msg}") from exc

    if not isinstance(decoded, dict):
        raise ParseError("Payload NDJSON deve ser um objeto JSON.")

    if "ack" in decoded:
        cmd_name = str(decoded["ack"]).strip()
        if not cmd_name:
            raise ParseError("Campo 'ack' vazio.")
        raw_ok = decoded.get("ok", True)
        if not isinstance(raw_ok, bool):
            raise ParseError("Campo 'ok' em ACK deve ser booleano.")
        raw_msg = decoded.get("message")
        msg = str(raw_msg) if raw_msg is not None else None
        return ArduinoAck(cmd=cmd_name, ok=raw_ok, message=msg)

    if "temp" not in decoded or "fan" not in decoded:
        raise ParseError("Telemetria deve conter os campos obrigatórios 'temp' e 'fan'.")

    temp: float | None
    sensor_error: str | None = None
    if decoded["temp"] is None:
        # Falha do sensor reportada pelo Arduino: "temp": null (+ "err" opcional).
        temp = None
        raw_err = decoded.get("err")
        sensor_error = str(raw_err).strip() if raw_err is not None else ""
        sensor_error = sensor_error or "sensor"
    else:
        temp = _extract_finite_float(decoded, "temp")
        if not (MIN_VALID_TEMP <= temp <= MAX_VALID_TEMP):
            raise ParseError(
                f"Temperatura fora da faixa plausível "
                f"({MIN_VALID_TEMP} a {MAX_VALID_TEMP} °C): {temp}"
            )

    raw_fan = decoded["fan"]
    if not isinstance(raw_fan, bool):
        raise ParseError("Campo 'fan' deve ser booleano (true/false).")

    limit: float | None = None
    if decoded.get("limit") is not None:
        raw_limit = _extract_finite_float(decoded, "limit")
        if not (MIN_VALID_TEMP <= raw_limit <= MAX_VALID_TEMP):
            raise ParseError(f"Campo 'limit' fora da faixa válida: {raw_limit}")
        limit = round(raw_limit, 2)

    mode: ControlMode | None = None
    if decoded.get("mode") is not None:
        raw_mode = decoded["mode"]
        if not isinstance(raw_mode, str):
            raise ParseError("Campo 'mode' deve ser string ('AUTO' ou 'MANUAL').")
        normalized_mode = raw_mode.strip().upper()
        try:
            mode = ControlMode(normalized_mode)
        except ValueError as exc:
            raise ParseError(f"Modo inválido recebido: {raw_mode!r}") from exc

    return ArduinoTelemetry(
        temp=round(temp, 2) if temp is not None else None,
        fan=raw_fan,
        limit=limit,
        mode=mode,
        received_at=received_at or datetime.now(UTC),
        sensor_error=sensor_error,
    )


def serialize_command(command: ArduinoCommand) -> str:
    """Serializa um comando para NDJSON pronto para escrita na porta Serial."""
    payload: dict[str, Any] = {
        "cmd": command.cmd.value,
        "value": command.value,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
