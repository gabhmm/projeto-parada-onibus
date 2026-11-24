"""Testes unitários do parser NDJSON e serializador de comandos Serial."""

from __future__ import annotations

import pytest

from parada_onibus.domain.models import (
    ArduinoAck,
    ArduinoCommand,
    ArduinoTelemetry,
    CommandType,
    ControlMode,
)
from parada_onibus.domain.parser import (
    ParseError,
    parse_serial_line,
    serialize_command,
    validate_temperature_limit,
)


def test_parse_valid_full_telemetry() -> None:
    result = parse_serial_line('{"temp": 31.456, "fan": true, "limit": 29.5, "mode": "manual"}')
    assert isinstance(result, ArduinoTelemetry)
    assert result.temp == 31.46
    assert result.fan is True
    assert result.limit == 29.5
    assert result.mode == ControlMode.MANUAL
    assert result.received_at is not None


def test_parse_valid_minimal_telemetry() -> None:
    result = parse_serial_line('{"temp": 27, "fan": false}')
    assert isinstance(result, ArduinoTelemetry)
    assert result.temp == 27.0
    assert result.fan is False
    assert result.limit is None
    assert result.mode is None


def test_parse_sensor_failure_from_firmware() -> None:
    result = parse_serial_line('{"temp":null,"fan":true,"limit":30.0,"mode":"AUTO","err":"dht"}')
    assert isinstance(result, ArduinoTelemetry)
    assert result.temp is None
    assert result.sensor_fault is True
    assert result.sensor_error == "dht"
    assert result.fan is True
    assert result.limit == 30.0


def test_parse_sensor_failure_without_err_field() -> None:
    result = parse_serial_line('{"temp": null, "fan": false}')
    assert isinstance(result, ArduinoTelemetry)
    assert result.sensor_fault is True
    assert result.sensor_error == "sensor"


def test_parse_valid_reading_has_no_sensor_fault() -> None:
    result = parse_serial_line('{"temp":25.0,"fan":false,"limit":30.0,"mode":"AUTO"}')
    assert isinstance(result, ArduinoTelemetry)
    assert result.sensor_fault is False
    assert result.sensor_error is None


def test_parse_optional_ack() -> None:
    result = parse_serial_line('{"ack": "set_limit", "ok": true}')
    assert isinstance(result, ArduinoAck)
    assert result.cmd == "set_limit"
    assert result.ok is True


@pytest.mark.parametrize(
    "bad_line",
    [
        "",
        "   ",
        "booting arduino...",
        '{"temp": 30.0',
        "[1, 2, 3]",
        '{"temp": 30.0}',
        '{"fan": true}',
        '{"temp": "quente", "fan": true}',
        '{"temp": true, "fan": true}',
        '{"temp": NaN, "fan": false}',
        '{"temp": Infinity, "fan": false}',
        '{"temp": 150.0, "fan": true}',
        '{"temp": -50.0, "fan": false}',
        '{"temp": 28.0, "fan": "yes"}',
        '{"temp": 28.0, "fan": true, "mode": "TURBO"}',
    ],
)
def test_parse_rejects_invalid_lines(bad_line: str) -> None:
    with pytest.raises(ParseError):
        parse_serial_line(bad_line)


def test_serialize_commands_ndjson() -> None:
    assert (
        serialize_command(ArduinoCommand(CommandType.SET_LIMIT, 28.5))
        == '{"cmd":"set_limit","value":28.5}\n'
    )
    assert (
        serialize_command(ArduinoCommand(CommandType.SET_FAN, False))
        == '{"cmd":"set_fan","value":false}\n'
    )
    assert (
        serialize_command(ArduinoCommand(CommandType.SET_MODE, "AUTO"))
        == '{"cmd":"set_mode","value":"AUTO"}\n'
    )


def test_validate_temperature_limit_bounds() -> None:
    assert validate_temperature_limit(30) == 30.0
    assert validate_temperature_limit(28.456) == 28.46
    with pytest.raises(ValueError):
        validate_temperature_limit(10.0)
    with pytest.raises(ValueError):
        validate_temperature_limit(55.0)
    with pytest.raises(ValueError):
        validate_temperature_limit(True)  # type: ignore[arg-type]
