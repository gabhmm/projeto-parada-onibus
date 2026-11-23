"""Testes unitários do motor de derivação das paradas virtuais (Paradas 2 e 3)."""

from __future__ import annotations

from datetime import UTC, datetime

from parada_onibus.domain.deriver import VirtualStopDeriver
from parada_onibus.domain.models import ControlMode, StopStatus
from parada_onibus.infrastructure.config import default_stops


def test_deriver_applies_factors_and_independent_thresholds() -> None:
    deriver = VirtualStopDeriver(default_stops())
    now = datetime.now(UTC)

    # 29.0°C na Parada 1 -> Parada 2: 30.45°C (>= 30 liga), Parada 3: 27.55°C (< 30 desliga)
    readings = deriver.derive_from_real(29.0, now)
    assert len(readings) == 2
    by_id = {r.stop_id: r for r in readings}

    assert by_id["parada-2"].temperature == 30.45
    assert by_id["parada-2"].ventilation is True
    assert by_id["parada-3"].temperature == 27.55
    assert by_id["parada-3"].ventilation is False


def test_deriver_manual_override_and_return_to_auto() -> None:
    deriver = VirtualStopDeriver(default_stops())
    now = datetime.now(UTC)

    deriver.derive_from_real(32.0, now)
    state_off = deriver.set_ventilation_manual_off("parada-2", connected=True)
    assert state_off.mode == ControlMode.MANUAL
    assert state_off.ventilation is False
    assert state_off.status == StopStatus.NORMAL

    # Nova leitura ainda mais quente deve respeitar override manual
    readings = deriver.derive_from_real(35.0, now)
    p2 = next(r for r in readings if r.stop_id == "parada-2")
    assert p2.ventilation is False
    assert p2.mode == ControlMode.MANUAL

    # Ao voltar para AUTO, reavalia imediatamente e liga a ventilação
    state_auto = deriver.set_mode_auto("parada-2", connected=True)
    assert state_auto.mode == ControlMode.AUTO
    assert state_auto.ventilation is True
    assert state_auto.status == StopStatus.VENTILANDO


def test_deriver_limit_change_reevaluates_and_disconnect_status() -> None:
    deriver = VirtualStopDeriver(default_stops())
    deriver.derive_from_real(29.0, datetime.now(UTC))

    # Parada 3 está em 27.55°C; abaixando limite para 27.0°C deve acionar ventilação
    updated = deriver.set_limit("parada-3", 27.0, connected=True)
    assert updated.temp_limit == 27.0
    assert updated.ventilation is True
    assert updated.status == StopStatus.VENTILANDO

    # Quando desconectado, reporta DESCONECTADO e ventilação False
    disconnected = deriver.get_state("parada-3", connected=False)
    assert disconnected.status == StopStatus.DESCONECTADO
    assert disconnected.ventilation is False
