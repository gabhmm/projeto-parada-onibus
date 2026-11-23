"""Testes de integração da API FastAPI, controles individuais, CSV, resiliência e SSE."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from parada_onibus.domain.models import AppConfig, CommandType
from parada_onibus.web.app import create_app
from tests.conftest import FakeTransport


def test_dashboard_static_and_health(test_config: AppConfig) -> None:
    transport = FakeTransport()
    app = create_app(test_config, transport=transport)

    with TestClient(app) as client:
        res_root = client.get("/")
        assert res_root.status_code == 200
        assert "Central de Paradas de Ônibus" in res_root.text

        res_chart = client.get("/static/vendor/chart.umd.min.js")
        assert res_chart.status_code == 200
        assert len(res_chart.content) > 50_000

        res_health = client.get("/api/health")
        assert res_health.status_code == 200
        assert res_health.json()["status"] == "ok"


def test_telemetry_derivation_commands_and_csv(test_config: AppConfig) -> None:
    transport = FakeTransport(initially_connected=True)
    app = create_app(test_config, transport=transport)

    with TestClient(app) as client:
        # Linha corrompida não derruba o servidor
        transport.emit_line("RUIDO_SERIAL_NAO_JSON")
        transport.emit_line('{"temp": 30.0, "fan": true, "limit": 30.0, "mode": "AUTO"}')

        state = client.get("/api/state").json()
        assert state["summary"]["connected"] is True
        stops_by_id = {s["stop_id"]: s for s in state["stops"]}
        assert stops_by_id["parada-1"]["temperature"] == 30.0
        assert stops_by_id["parada-1"]["status"] == "VENTILANDO"
        assert stops_by_id["parada-2"]["temperature"] == 31.5
        assert stops_by_id["parada-2"]["status"] == "VENTILANDO"
        assert stops_by_id["parada-3"]["temperature"] == 28.5
        assert stops_by_id["parada-3"]["status"] == "NORMAL"

        # Comando na Parada 1 deve ir ao transporte Serial
        res_limit = client.post("/api/stops/parada-1/limit", json={"limit": 28.0})
        assert res_limit.status_code == 200
        assert transport.sent_commands[-1].cmd == CommandType.SET_LIMIT
        assert transport.sent_commands[-1].value == 28.0

        res_off = client.post("/api/stops/parada-1/ventilation", json={"action": "turn_off"})
        assert res_off.status_code == 200
        assert transport.sent_commands[-1].cmd == CommandType.SET_FAN
        assert transport.sent_commands[-1].value is False

        # Comando na Parada 2 (Simulada) NÃO vai para a Serial, atua localmente
        cmd_count_before = len(transport.sent_commands)
        res_p2_off = client.post("/api/stops/parada-2/ventilation", json={"action": "turn_off"})
        assert res_p2_off.status_code == 200
        assert res_p2_off.json()["state"]["mode"] == "MANUAL"
        assert res_p2_off.json()["state"]["ventilation"] is False
        assert len(transport.sent_commands) == cmd_count_before

        # Validações de erro (404 / 400)
        assert client.post("/api/stops/parada-99/limit", json={"limit": 30.0}).status_code == 404
        assert client.post("/api/stops/parada-2/limit", json={"limit": 99.0}).status_code == 400
        assert (
            client.post("/api/stops/parada-2/ventilation", json={"action": "turbo"}).status_code
            == 400
        )

        # Exportação CSV geral e filtrada
        csv_all = client.get("/api/export/csv")
        assert csv_all.status_code == 200
        assert "text/csv" in csv_all.headers["content-type"]
        assert "parada-1" in csv_all.text
        assert "parada-2" in csv_all.text
        assert "parada-3" in csv_all.text

        csv_p3 = client.get("/api/export/csv?stop_id=parada-3")
        assert csv_p3.status_code == 200
        assert "parada-3" in csv_p3.text
        assert "parada-2" not in csv_p3.text


def test_disconnect_watchdog_and_503_when_offline(test_config: AppConfig) -> None:
    short_timeout_cfg = replace(test_config, serial_timeout_seconds=0.35)
    transport = FakeTransport(initially_connected=True)
    app = create_app(short_timeout_cfg, transport=transport)

    with TestClient(app) as client:
        transport.emit_line('{"temp": 31.0, "fan": true}')
        assert client.get("/api/state").json()["summary"]["connected"] is True

        # Aguarda estourar o watchdog sem novos dados
        time.sleep(0.65)
        state = client.get("/api/state").json()
        assert state["summary"]["connected"] is False
        assert all(s["status"] == "DESCONECTADO" for s in state["stops"])

        # Sem Arduino conectado, comando na Parada 1 deve retornar 503
        transport.set_connected(False, "cabo removido")
        res = client.post("/api/stops/parada-1/limit", json={"limit": 29.0})
        assert res.status_code == 503


def test_broadcaster_publishes_sse_events(test_config: AppConfig) -> None:
    transport = FakeTransport(initially_connected=True)
    app = create_app(test_config, transport=transport)

    with TestClient(app) as client:
        manager = app.state.manager
        queue = manager.broadcaster.subscribe()
        try:
            transport.emit_line('{"temp": 30.5, "fan": true}')
            # Aguarda o loop do TestClient processar o call_soon_threadsafe
            client.get("/api/health")

            received_events: list[str] = []
            while True:
                try:
                    msg = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if msg is not None:
                    received_events.append(msg.event)

            assert "reading" in received_events
        finally:
            manager.broadcaster.unsubscribe(queue)
