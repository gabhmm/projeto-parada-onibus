"""Teste ponta a ponta (E2E) do Modo Simulador Completo sem hardware conectado."""

from __future__ import annotations

import time
from dataclasses import replace

from fastapi.testclient import TestClient

from parada_onibus.domain.models import AppConfig
from parada_onibus.web.app import create_app


def test_simulator_mode_e2e_generates_3_stops_and_handles_commands(
    test_config: AppConfig,
) -> None:
    sim_cfg = replace(
        test_config,
        mode="simulator",
        simulator_interval_seconds=0.04,
        db_persist_interval_seconds=0.0,
    )
    app = create_app(sim_cfg)

    with TestClient(app) as client:
        # Deixa o simulador gerar diversas leituras cruzando 30 °C
        time.sleep(0.35)

        state = client.get("/api/state").json()
        assert state["summary"]["connected"] is True
        assert state["summary"]["operation_mode"] == "simulator"
        assert len(state["stops"]) == 3

        for stop_id in ("parada-1", "parada-2", "parada-3"):
            assert len(state["history"][stop_id]) >= 3

        # Comando na Parada 1 simulada deve funcionar ponta a ponta
        res_limit = client.post("/api/stops/parada-1/limit", json={"limit": 27.5})
        assert res_limit.status_code == 200
        assert res_limit.json()["state"]["temp_limit"] == 27.5

        res_off = client.post("/api/stops/parada-1/ventilation", json={"action": "turn_off"})
        assert res_off.status_code == 200
        assert res_off.json()["state"]["mode"] == "MANUAL"
        assert res_off.json()["state"]["ventilation"] is False

        # Verifica CSV completo gerado a partir das leituras do simulador
        csv_res = client.get("/api/export/csv")
        assert csv_res.status_code == 200
        lines = [ln for ln in csv_res.text.strip().splitlines() if ln]
        assert len(lines) >= 10  # cabeçalho + >= 9 leituras (3 ciclos x 3 paradas)
