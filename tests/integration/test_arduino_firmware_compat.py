"""Compatibilidade com o firmware real da Parada 1 (DHT11 + relé + botão + RGB).

As linhas usadas aqui são exatamente as que o sketch emite via ``Serial.print``.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from parada_onibus.application import manager as manager_module
from parada_onibus.domain.models import AppConfig, ArduinoCommand, CommandType, ControlMode
from parada_onibus.web.app import create_app
from tests.conftest import FakeTransport

FW_OK = '{"temp":25.0,"fan":false,"limit":30.0,"mode":"AUTO"}'
FW_HOT = '{"temp":31.0,"fan":true,"limit":30.0,"mode":"AUTO"}'
FW_DHT_FAIL = '{"temp":null,"fan":true,"limit":30.0,"mode":"AUTO","err":"dht"}'


@pytest.fixture(autouse=True)
def _fast_resync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove esperas de tempo real da ressincronização para testes determinísticos."""
    monkeypatch.setattr(manager_module, "RESYNC_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(manager_module, "COMMAND_GRACE_SECONDS", 0.0)


def _fw(temp: float, fan: bool, limit: float, mode: str) -> str:
    fan_txt = "true" if fan else "false"
    return f'{{"temp":{temp:.1f},"fan":{fan_txt},"limit":{limit:.1f},"mode":"{mode}"}}'


def _stops(client: TestClient) -> dict[str, dict[str, Any]]:
    return {s["stop_id"]: s for s in client.get("/api/state").json()["stops"]}


def _event_types(client: TestClient) -> list[str]:
    return [e["event_type"] for e in client.get("/api/events").json()["events"]]


def _cmds(transport: FakeTransport) -> list[tuple[CommandType, object]]:
    return [(c.cmd, c.value) for c in transport.sent_commands]


def test_dht_failure_keeps_connection_and_shows_sensor_fault(test_config: AppConfig) -> None:
    transport = FakeTransport()
    app = create_app(test_config, transport=transport)

    with TestClient(app) as client:
        transport.emit_line(FW_HOT)
        readings_before = len(client.get("/api/stops/parada-1/readings").json()["readings"])

        transport.emit_line(FW_DHT_FAIL)

        state = client.get("/api/state").json()
        assert state["summary"]["connected"] is True
        assert state["summary"]["sensor_fault"] is True
        assert state["summary"]["sensor_error"] == "dht"

        stops = {s["stop_id"]: s for s in state["stops"]}
        p1 = stops["parada-1"]
        assert p1["status"] == "FALHA_SENSOR"
        assert p1["temperature"] is None
        assert p1["ventilation"] is True  # fail-safe do firmware
        for virtual in ("parada-2", "parada-3"):
            assert stops[virtual]["status"] == "FALHA_SENSOR"
            assert stops[virtual]["temperature"] is None
            assert stops[virtual]["ventilation"] is True  # espelha o fail-safe em AUTO

        # Sem temperatura nada é gravado na tabela de leituras (coluna NOT NULL).
        readings_after = len(client.get("/api/stops/parada-1/readings").json()["readings"])
        assert readings_after == readings_before
        assert "SENSOR_FAULT" in _event_types(client)

        transport.emit_line(FW_OK)
        stops = _stops(client)
        assert stops["parada-1"]["status"] == "NORMAL"
        assert stops["parada-1"]["temperature"] == 25.0
        assert client.get("/api/state").json()["summary"]["sensor_fault"] is False
        assert "SENSOR_RECOVERED" in _event_types(client)


def test_dht_failure_does_not_trigger_disconnect_watchdog(test_config: AppConfig) -> None:
    cfg = replace(test_config, serial_timeout_seconds=0.6)
    transport = FakeTransport()
    app = create_app(cfg, transport=transport)

    with TestClient(app) as client:
        for _ in range(8):
            transport.emit_line(FW_DHT_FAIL)
            time.sleep(0.15)
        assert client.get("/api/state").json()["summary"]["connected"] is True
        assert "SERIAL_DISCONNECTED" not in _event_types(client)


def test_virtual_stop_in_manual_off_stays_off_during_sensor_fault(
    test_config: AppConfig,
) -> None:
    transport = FakeTransport()
    app = create_app(test_config, transport=transport)

    with TestClient(app) as client:
        transport.emit_line(FW_OK)
        client.post("/api/stops/parada-2/ventilation", json={"action": "turn_off"})
        transport.emit_line(FW_DHT_FAIL)
        p2 = _stops(client)["parada-2"]
        assert p2["status"] == "FALHA_SENSOR"
        assert p2["ventilation"] is False


def test_saved_settings_are_restored_after_raspi_restart(test_config: AppConfig) -> None:
    first = FakeTransport()
    with TestClient(create_app(test_config, transport=first)) as client:
        first.emit_line(FW_OK)
        assert client.post("/api/stops/parada-1/limit", json={"limit": 26.0}).status_code == 200
        res = client.post("/api/stops/parada-1/ventilation", json={"action": "turn_off"})
        assert res.status_code == 200

    # Servidor reinicia; abrir a porta reseta o Arduino (DTR) -> volta com 30.0/AUTO.
    second = FakeTransport()
    with TestClient(create_app(test_config, transport=second)) as client:
        second.emit_line(FW_OK)
        assert _cmds(second) == [
            (CommandType.SET_LIMIT, 26.0),
            (CommandType.SET_FAN, False),
        ]

        # Arduino aplicou: telemetria confirma -> ressincronização concluída.
        second.emit_line(_fw(25.0, False, 26.0, "MANUAL"))
        second.emit_line(_fw(25.0, False, 26.0, "MANUAL"))
        assert len(second.sent_commands) == 2
        assert "STATE_RESYNC" in _event_types(client)
        p1 = _stops(client)["parada-1"]
        assert p1["temp_limit"] == 26.0
        assert p1["mode"] == "MANUAL"


def test_resync_after_serial_reconnect(test_config: AppConfig) -> None:
    transport = FakeTransport()
    with TestClient(create_app(test_config, transport=transport)) as client:
        transport.emit_line(FW_OK)
        client.post("/api/stops/parada-1/limit", json={"limit": 27.5})
        transport.emit_line(_fw(25.0, False, 27.5, "AUTO"))
        transport.sent_commands.clear()

        transport.set_connected(False, "cabo USB removido")
        assert client.get("/api/state").json()["summary"]["connected"] is False

        transport.set_connected(True, "reconectado")
        transport.emit_line(FW_OK)  # Arduino reiniciou com o limite padrão
        assert _cmds(transport) == [(CommandType.SET_LIMIT, 27.5)]


def test_resync_gives_up_and_adopts_arduino_values(test_config: AppConfig) -> None:
    transport = FakeTransport()
    with TestClient(create_app(test_config, transport=transport)) as client:
        transport.emit_line(FW_OK)
        client.post("/api/stops/parada-1/limit", json={"limit": 22.0})
        transport.sent_commands.clear()

        # Arduino "surdo": nunca aplica o limite.
        for _ in range(manager_module.RESYNC_MAX_ATTEMPTS + 3):
            transport.emit_line(FW_OK)

        assert len(transport.sent_commands) == manager_module.RESYNC_MAX_ATTEMPTS
        assert all(c.cmd == CommandType.SET_LIMIT for c in transport.sent_commands)
        settings = client.app.state.manager.repository.load_stop_settings()  # type: ignore[attr-defined]
        assert settings["parada-1"].temp_limit == 30.0

        # Sem laço: novas telemetrias iguais não geram mais comandos.
        transport.emit_line(FW_OK)
        assert len(transport.sent_commands) == manager_module.RESYNC_MAX_ATTEMPTS


def test_physical_button_change_is_adopted_not_reverted(test_config: AppConfig) -> None:
    transport = FakeTransport()
    with TestClient(create_app(test_config, transport=transport)) as client:
        transport.emit_line(FW_OK)  # já bate com o padrão salvo -> nada a sincronizar
        assert transport.sent_commands == []

        # Clique curto no botão: MANUAL + cooler ligado.
        transport.emit_line(_fw(25.0, True, 30.0, "MANUAL"))
        transport.emit_line(_fw(25.0, True, 30.0, "MANUAL"))
        assert transport.sent_commands == []

        settings = client.app.state.manager.repository.load_stop_settings()  # type: ignore[attr-defined]
        assert settings["parada-1"].mode == ControlMode.MANUAL
        assert settings["parada-1"].manual_fan is True

        # Clique longo: volta ao AUTO.
        transport.emit_line(FW_OK)
        settings = client.app.state.manager.repository.load_stop_settings()  # type: ignore[attr-defined]
        assert settings["parada-1"].mode == ControlMode.AUTO
        assert transport.sent_commands == []


def test_arduino_reboot_while_online_restores_limit(test_config: AppConfig) -> None:
    transport = FakeTransport()
    with TestClient(create_app(test_config, transport=transport)) as client:
        transport.emit_line(FW_OK)
        client.post("/api/stops/parada-1/limit", json={"limit": 28.0})
        transport.emit_line(_fw(25.0, False, 28.0, "AUTO"))
        transport.sent_commands.clear()

        # Queda de energia no relé reinicia o Arduino sem fechar a porta Serial.
        transport.emit_line(FW_OK)
        assert transport.sent_commands == [ArduinoCommand(CommandType.SET_LIMIT, 28.0)]


def test_rejected_ack_is_recorded(test_config: AppConfig) -> None:
    transport = FakeTransport()
    with TestClient(create_app(test_config, transport=transport)) as client:
        transport.emit_line(FW_OK)
        transport.emit_line('{"ack":"set_limit","ok":false}')
        assert "COMMAND_REJECTED" in _event_types(client)
