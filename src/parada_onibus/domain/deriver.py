"""Motor de derivação em tempo real das paradas virtuais (Paradas 2 e 3)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from parada_onibus.domain.models import (
    ControlMode,
    StopConfig,
    StopOrigin,
    StopReading,
    StopState,
    StopStatus,
    StoredStopSetting,
)
from parada_onibus.domain.parser import validate_temperature_limit


@dataclass(slots=True)
class _VirtualStopRuntime:
    """Estado interno mutável de uma parada simulada."""

    config: StopConfig
    temp_limit: float
    mode: ControlMode
    manual_fan: bool = False
    last_temperature: float | None = None
    last_ventilation: bool = False
    last_updated: datetime | None = None


class VirtualStopDeriver:
    """Calcula temperaturas derivadas e aplica regras independentes para paradas simuladas."""

    def __init__(
        self,
        virtual_stops: Iterable[StopConfig],
        stored_settings: dict[str, StoredStopSetting] | None = None,
    ) -> None:
        settings = stored_settings or {}
        self._stops: dict[str, _VirtualStopRuntime] = {}
        for cfg in virtual_stops:
            if cfg.origin != StopOrigin.SIMULADA:
                continue
            saved = settings.get(cfg.stop_id)
            limit = saved.temp_limit if saved else cfg.initial_limit
            mode = saved.mode if saved else ControlMode.AUTO
            manual_fan = saved.manual_fan if saved else False
            self._stops[cfg.stop_id] = _VirtualStopRuntime(
                config=cfg,
                temp_limit=limit,
                mode=mode,
                manual_fan=manual_fan,
                last_ventilation=manual_fan if mode == ControlMode.MANUAL else False,
            )

    def has_stop(self, stop_id: str) -> bool:
        return stop_id in self._stops

    def stop_ids(self) -> tuple[str, ...]:
        return tuple(self._stops.keys())

    def derive_from_real(self, real_temp: float, timestamp: datetime) -> list[StopReading]:
        """Gera novas leituras para todas as paradas simuladas a partir da temperatura real."""
        readings: list[StopReading] = []
        for runtime in self._stops.values():
            derived_temp = round(real_temp * runtime.config.derivation_factor, 2)
            ventilation = self._evaluate_ventilation(runtime, derived_temp)
            runtime.last_temperature = derived_temp
            runtime.last_ventilation = ventilation
            runtime.last_updated = timestamp
            readings.append(
                StopReading(
                    timestamp=timestamp,
                    stop_id=runtime.config.stop_id,
                    stop_name=runtime.config.name,
                    origin=runtime.config.origin,
                    temperature=derived_temp,
                    temp_limit=runtime.temp_limit,
                    ventilation=ventilation,
                    mode=runtime.mode,
                )
            )
        return readings

    def set_limit(self, stop_id: str, new_limit: float, *, connected: bool) -> StopState:
        """Atualiza o limite de temperatura de uma parada virtual e reavalia ventilação."""
        runtime = self._require(stop_id)
        validated = validate_temperature_limit(new_limit)
        runtime.temp_limit = validated
        if runtime.last_temperature is not None:
            runtime.last_ventilation = self._evaluate_ventilation(runtime, runtime.last_temperature)
        return self.get_state(stop_id, connected=connected)

    def set_ventilation_manual_off(self, stop_id: str, *, connected: bool) -> StopState:
        """Ativa override manual desligando a ventilação da parada virtual."""
        runtime = self._require(stop_id)
        runtime.mode = ControlMode.MANUAL
        runtime.manual_fan = False
        runtime.last_ventilation = False
        return self.get_state(stop_id, connected=connected)

    def set_mode_auto(self, stop_id: str, *, connected: bool) -> StopState:
        """Retorna a parada virtual ao modo automático e reavalia a regra temp >= limite."""
        runtime = self._require(stop_id)
        runtime.mode = ControlMode.AUTO
        if runtime.last_temperature is not None:
            runtime.last_ventilation = self._evaluate_ventilation(runtime, runtime.last_temperature)
        else:
            runtime.last_ventilation = False
        return self.get_state(stop_id, connected=connected)

    def get_state(self, stop_id: str, *, connected: bool) -> StopState:
        runtime = self._require(stop_id)
        if not connected or runtime.last_temperature is None:
            status = StopStatus.DESCONECTADO
        elif runtime.last_ventilation:
            status = StopStatus.VENTILANDO
        else:
            status = StopStatus.NORMAL
        return StopState(
            stop_id=runtime.config.stop_id,
            name=runtime.config.name,
            origin=runtime.config.origin,
            derivation_factor=runtime.config.derivation_factor,
            temperature=runtime.last_temperature,
            temp_limit=runtime.temp_limit,
            ventilation=runtime.last_ventilation if connected else False,
            mode=runtime.mode,
            status=status,
            last_updated=runtime.last_updated,
        )

    def get_all_states(self, *, connected: bool) -> list[StopState]:
        return [self.get_state(stop_id, connected=connected) for stop_id in self._stops]

    def _require(self, stop_id: str) -> _VirtualStopRuntime:
        runtime = self._stops.get(stop_id)
        if runtime is None:
            raise KeyError(f"Parada virtual desconhecida: {stop_id}")
        return runtime

    @staticmethod
    def _evaluate_ventilation(runtime: _VirtualStopRuntime, temperature: float) -> bool:
        if runtime.mode == ControlMode.MANUAL:
            return runtime.manual_fan
        return temperature >= runtime.temp_limit
