"""Orquestrador central das 3 paradas: telemetria, derivação, watchdog, comandos e persistência."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from parada_onibus.application.broadcaster import SSEBroadcaster
from parada_onibus.domain.deriver import VirtualStopDeriver
from parada_onibus.domain.models import (
    AppConfig,
    ArduinoAck,
    ArduinoCommand,
    ArduinoTelemetry,
    CommandResult,
    CommandType,
    ControlMode,
    EventType,
    NetworkSummary,
    StopConfig,
    StopOrigin,
    StopReading,
    StopState,
    StopStatus,
    SystemEvent,
)
from parada_onibus.domain.parser import ParseError, parse_serial_line, validate_temperature_limit
from parada_onibus.domain.ports import DataRepository, TelemetryTransport

logger = logging.getLogger(__name__)


class StopNetworkManager:
    """Coordena o estado em tempo real da Parada 1 (Real) e Paradas 2 e 3 (Simuladas)."""

    def __init__(
        self,
        config: AppConfig,
        transport: TelemetryTransport,
        repository: DataRepository,
        broadcaster: SSEBroadcaster,
    ) -> None:
        self._config = config
        self._transport = transport
        self._repo = repository
        self._broadcaster = broadcaster
        self._lock = threading.RLock()

        real_stops = [s for s in config.stops if s.origin == StopOrigin.REAL]
        if len(real_stops) != 1:
            raise ValueError("É obrigatória exatamente 1 parada REAL na configuração.")
        self._real_cfg: StopConfig = real_stops[0]
        self._ordered_ids: tuple[str, ...] = tuple(s.stop_id for s in config.stops)

        stored_settings = self._repo.load_stop_settings()
        real_saved = stored_settings.get(self._real_cfg.stop_id)

        self._real_temp: float | None = None
        self._real_fan: bool = False
        self._real_limit: float = (
            real_saved.temp_limit if real_saved else self._real_cfg.initial_limit
        )
        self._real_mode: ControlMode = real_saved.mode if real_saved else ControlMode.AUTO
        self._real_last_updated: datetime | None = None

        self._deriver = VirtualStopDeriver(config.stops, stored_settings=stored_settings)

        self._telemetry_online: bool = False
        self._last_telemetry_monotonic: float | None = None
        self._last_persist_monotonic: float = 0.0
        self._prev_ventilation: dict[str, bool] = {}

        self._watchdog_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def broadcaster(self) -> SSEBroadcaster:
        return self._broadcaster

    @property
    def repository(self) -> DataRepository:
        return self._repo

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def is_online(self) -> bool:
        with self._lock:
            return self._telemetry_online

    def has_stop(self, stop_id: str) -> bool:
        return stop_id in self._ordered_ids

    async def start(self) -> None:
        """Inicia o transporte Serial/Simulador e o watchdog de desconexão."""
        if self._running:
            return
        self._running = True
        loop = asyncio.get_running_loop()
        self._broadcaster.bind_loop(loop)
        self._transport.start(
            on_line=self.handle_raw_line,
            on_status=self._handle_transport_status,
        )
        self._watchdog_task = loop.create_task(self._watchdog_loop(), name="serial-watchdog")

    async def stop(self) -> None:
        """Encerra transporte, watchdog e assinantes SSE de forma limpa."""
        self._running = False
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        self._transport.stop()
        self._broadcaster.close_all()

    def handle_raw_line(self, raw_line: str) -> None:
        """Processa uma linha NDJSON recebida do transporte (Serial ou Simulador)."""
        now = datetime.now(UTC)
        try:
            parsed = parse_serial_line(raw_line, received_at=now)
        except ParseError as exc:
            logger.warning("Linha serial descartada (%s): %r", exc, raw_line[:120])
            return

        if isinstance(parsed, ArduinoAck):
            logger.info("ACK recebido do Arduino: cmd=%s ok=%s", parsed.cmd, parsed.ok)
            return

        self._apply_telemetry(parsed, now)

    def _apply_telemetry(self, telemetry: ArduinoTelemetry, now: datetime) -> None:
        events_to_emit: list[SystemEvent] = []
        transition_detected = False

        with self._lock:
            was_online = self._telemetry_online
            self._telemetry_online = True
            self._last_telemetry_monotonic = time.monotonic()

            if not was_online:
                events_to_emit.append(
                    SystemEvent(
                        timestamp=now,
                        stop_id=self._real_cfg.stop_id,
                        event_type=EventType.SERIAL_CONNECTED,
                        message=(
                            f"Telemetria ativa ({self._transport.mode_name}) na "
                            f"{self._real_cfg.name}."
                        ),
                    )
                )

            self._real_temp = telemetry.temp
            self._real_fan = telemetry.fan
            if telemetry.limit is not None:
                self._real_limit = telemetry.limit
            if telemetry.mode is not None:
                self._real_mode = telemetry.mode
            self._real_last_updated = now

            real_reading = StopReading(
                timestamp=now,
                stop_id=self._real_cfg.stop_id,
                stop_name=self._real_cfg.name,
                origin=self._real_cfg.origin,
                temperature=self._real_temp,
                temp_limit=self._real_limit,
                ventilation=self._real_fan,
                mode=self._real_mode,
            )
            virtual_readings = self._deriver.derive_from_real(self._real_temp, now)
            all_readings = [real_reading, *virtual_readings]

            for reading in all_readings:
                prev = self._prev_ventilation.get(reading.stop_id)
                if prev is not None and prev != reading.ventilation:
                    transition_detected = True
                    ev_type = (
                        EventType.VENTILATION_ON
                        if reading.ventilation
                        else EventType.VENTILATION_OFF
                    )
                    action_txt = "ligada" if reading.ventilation else "desligada"
                    events_to_emit.append(
                        SystemEvent(
                            timestamp=now,
                            stop_id=reading.stop_id,
                            event_type=ev_type,
                            message=(
                                f"{reading.stop_name}: ventilação {action_txt} "
                                f"({reading.temperature:.2f} °C / "
                                f"limite {reading.temp_limit:.1f} °C)."
                            ),
                        )
                    )
                self._prev_ventilation[reading.stop_id] = reading.ventilation

            mono_now = time.monotonic()
            should_persist = (
                transition_detected
                or not was_online
                or (mono_now - self._last_persist_monotonic)
                >= self._config.db_persist_interval_seconds
            )
            if should_persist:
                self._last_persist_monotonic = mono_now

            summary = self._build_summary_locked()
            states = self._build_all_states_locked()

        if should_persist:
            try:
                self._repo.save_readings(all_readings)
            except Exception:
                logger.exception("Erro ao persistir leituras no SQLite.")

        for event in events_to_emit:
            self._record_and_publish_event(event)

        if not was_online:
            self._broadcaster.publish(
                "status",
                {
                    "connected": True,
                    "operation_mode": self._transport.mode_name,
                    "summary": summary.to_dict(),
                    "stops": [s.to_dict() for s in states],
                },
            )

        self._broadcaster.publish(
            "reading",
            {
                "summary": summary.to_dict(),
                "stops": [s.to_dict() for s in states],
                "readings": [r.to_dict() for r in all_readings],
            },
        )

    def _handle_transport_status(self, connected: bool, detail: str) -> None:
        logger.info(
            "Status transporte (%s): connected=%s | %s",
            self._transport.mode_name,
            connected,
            detail,
        )
        if not connected:
            self._mark_disconnected(detail)

    def _mark_disconnected(self, reason: str) -> None:
        now = datetime.now(UTC)
        with self._lock:
            if not self._telemetry_online:
                return
            self._telemetry_online = False
            summary = self._build_summary_locked()
            states = self._build_all_states_locked()

        event = SystemEvent(
            timestamp=now,
            stop_id=self._real_cfg.stop_id,
            event_type=EventType.SERIAL_DISCONNECTED,
            message=f"Sem sinal da Parada 1 ({reason}). Derivação virtual pausada.",
        )
        self._record_and_publish_event(event)
        self._broadcaster.publish(
            "status",
            {
                "connected": False,
                "operation_mode": self._transport.mode_name,
                "reason": reason,
                "summary": summary.to_dict(),
                "stops": [s.to_dict() for s in states],
            },
        )

    async def _watchdog_loop(self) -> None:
        check_interval = min(1.0, max(0.2, self._config.serial_timeout_seconds / 3.0))
        while self._running:
            await asyncio.sleep(check_interval)
            should_disconnect = False
            with self._lock:
                if (
                    self._telemetry_online
                    and self._last_telemetry_monotonic is not None
                    and (time.monotonic() - self._last_telemetry_monotonic)
                    > self._config.serial_timeout_seconds
                ):
                    should_disconnect = True
            if should_disconnect:
                self._mark_disconnected(
                    f"timeout de {self._config.serial_timeout_seconds:.1f}s sem telemetria válida"
                )

    def set_stop_limit(self, stop_id: str, raw_limit: float) -> CommandResult:
        """Altera o limite de temperatura de uma parada (Real via Serial ou Simulada local)."""
        if not self.has_stop(stop_id):
            return CommandResult(
                ok=False,
                stop_id=stop_id,
                message=f"Parada não encontrada: {stop_id}",
                http_status=404,
            )

        try:
            validated = validate_temperature_limit(raw_limit)
        except ValueError as exc:
            return CommandResult(
                ok=False,
                stop_id=stop_id,
                message=str(exc),
                http_status=400,
            )

        now = datetime.now(UTC)
        if stop_id == self._real_cfg.stop_id:
            if not self._transport.is_connected:
                return CommandResult(
                    ok=False,
                    stop_id=stop_id,
                    message="Arduino desconectado. Não foi possível enviar o novo limite.",
                    http_status=503,
                )
            cmd = ArduinoCommand(cmd=CommandType.SET_LIMIT, value=validated)
            if not self._transport.send_command(cmd):
                return CommandResult(
                    ok=False,
                    stop_id=stop_id,
                    message="Falha ao gravar comando na porta Serial.",
                    http_status=503,
                )
            with self._lock:
                self._real_limit = validated
                mode = self._real_mode
                fan = self._real_fan
                state = self._build_real_state_locked()
            self._repo.save_stop_setting(stop_id, validated, mode, fan)
            self._record_and_publish_event(
                SystemEvent(
                    timestamp=now,
                    stop_id=stop_id,
                    event_type=EventType.COMMAND_SENT,
                    message=(
                        f"{self._real_cfg.name}: comando de limite {validated:.1f} °C "
                        "enviado ao Arduino."
                    ),
                    payload_json=json.dumps({"cmd": "set_limit", "value": validated}),
                )
            )
            self._publish_state_update()
            return CommandResult(
                ok=True,
                stop_id=stop_id,
                message=f"Limite da {self._real_cfg.name} atualizado para {validated:.1f} °C.",
                state=state,
            )

        # Parada simulada (Parada 2 ou Parada 3)
        with self._lock:
            state = self._deriver.set_limit(stop_id, validated, connected=self._telemetry_online)
            self._prev_ventilation[stop_id] = state.ventilation
        self._repo.save_stop_setting(
            stop_id,
            state.temp_limit,
            state.mode,
            state.ventilation if state.mode == ControlMode.MANUAL else False,
        )
        self._record_and_publish_event(
            SystemEvent(
                timestamp=now,
                stop_id=stop_id,
                event_type=EventType.LIMIT_CHANGED,
                message=f"{state.name}: limite alterado para {validated:.1f} °C.",
                payload_json=json.dumps({"limit": validated}),
            )
        )
        self._publish_state_update()
        return CommandResult(
            ok=True,
            stop_id=stop_id,
            message=f"Limite da {state.name} atualizado para {validated:.1f} °C.",
            state=state,
        )

    def set_stop_ventilation(self, stop_id: str, action: str) -> CommandResult:
        """Executa override manual (turn_off) ou retorno ao modo automático (auto)."""
        if not self.has_stop(stop_id):
            return CommandResult(
                ok=False,
                stop_id=stop_id,
                message=f"Parada não encontrada: {stop_id}",
                http_status=404,
            )

        normalized = action.strip().lower()
        if normalized not in ("turn_off", "auto"):
            return CommandResult(
                ok=False,
                stop_id=stop_id,
                message="Ação inválida. Utilize 'turn_off' ou 'auto'.",
                http_status=400,
            )

        now = datetime.now(UTC)
        if stop_id == self._real_cfg.stop_id:
            if not self._transport.is_connected:
                return CommandResult(
                    ok=False,
                    stop_id=stop_id,
                    message=(
                        "Arduino desconectado. Não foi possível enviar o comando de ventilação."
                    ),
                    http_status=503,
                )
            cmd = (
                ArduinoCommand(cmd=CommandType.SET_FAN, value=False)
                if normalized == "turn_off"
                else ArduinoCommand(cmd=CommandType.SET_MODE, value=ControlMode.AUTO.value)
            )
            if not self._transport.send_command(cmd):
                return CommandResult(
                    ok=False,
                    stop_id=stop_id,
                    message="Falha ao gravar comando de ventilação na porta Serial.",
                    http_status=503,
                )
            with self._lock:
                if normalized == "turn_off":
                    self._real_mode = ControlMode.MANUAL
                    self._real_fan = False
                else:
                    self._real_mode = ControlMode.AUTO
                limit = self._real_limit
                mode = self._real_mode
                fan = self._real_fan
                state = self._build_real_state_locked()
            self._repo.save_stop_setting(stop_id, limit, mode, fan)
            msg = (
                f"{self._real_cfg.name}: comando para desligar ventilação (MANUAL) "
                "enviado ao Arduino."
                if normalized == "turn_off"
                else f"{self._real_cfg.name}: retorno ao modo AUTOMÁTICO enviado ao Arduino."
            )
            self._record_and_publish_event(
                SystemEvent(
                    timestamp=now,
                    stop_id=stop_id,
                    event_type=EventType.COMMAND_SENT,
                    message=msg,
                    payload_json=json.dumps({"cmd": cmd.cmd.value, "value": cmd.value}),
                )
            )
            self._publish_state_update()
            return CommandResult(ok=True, stop_id=stop_id, message=msg, state=state)

        # Parada simulada (Parada 2 ou Parada 3)
        with self._lock:
            if normalized == "turn_off":
                state = self._deriver.set_ventilation_manual_off(
                    stop_id, connected=self._telemetry_online
                )
                ev_type = EventType.VENTILATION_OFF
                msg = f"{state.name}: ventilação desligada manualmente (modo MANUAL)."
            else:
                state = self._deriver.set_mode_auto(stop_id, connected=self._telemetry_online)
                ev_type = EventType.COMMAND_SENT
                msg = f"{state.name}: retornou ao modo AUTOMÁTICO."
            self._prev_ventilation[stop_id] = state.ventilation

        self._repo.save_stop_setting(
            stop_id,
            state.temp_limit,
            state.mode,
            False if state.mode == ControlMode.MANUAL else state.ventilation,
        )
        self._record_and_publish_event(
            SystemEvent(
                timestamp=now,
                stop_id=stop_id,
                event_type=ev_type,
                message=msg,
                payload_json=json.dumps({"action": normalized}),
            )
        )
        self._publish_state_update()
        return CommandResult(ok=True, stop_id=stop_id, message=msg, state=state)

    def get_snapshot(self) -> dict[str, Any]:
        """Monta snapshot completo da rede, estados das 3 paradas, histórico e eventos."""
        with self._lock:
            summary = self._build_summary_locked()
            states = self._build_all_states_locked()

        history: dict[str, list[dict[str, Any]]] = {
            stop_id: [
                r.to_dict()
                for r in self._repo.get_recent_readings(
                    stop_id, limit=self._config.chart_history_points
                )
            ]
            for stop_id in self._ordered_ids
        }
        events = [e.to_dict() for e in self._repo.get_recent_events(limit=30)]
        return {
            "summary": summary.to_dict(),
            "stops": [s.to_dict() for s in states],
            "history": history,
            "events": events,
        }

    def get_health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "connected": self._telemetry_online,
                "transport_connected": self._transport.is_connected,
                "operation_mode": self._transport.mode_name,
                "total_stops": len(self._ordered_ids),
            }

    def _publish_state_update(self) -> None:
        with self._lock:
            summary = self._build_summary_locked()
            states = self._build_all_states_locked()
        self._broadcaster.publish(
            "status",
            {
                "connected": summary.connected,
                "operation_mode": summary.operation_mode,
                "summary": summary.to_dict(),
                "stops": [s.to_dict() for s in states],
            },
        )

    def _record_and_publish_event(self, event: SystemEvent) -> None:
        try:
            self._repo.save_event(event)
        except Exception:
            logger.exception("Erro ao persistir evento no SQLite.")
        self._broadcaster.publish("event_log", event.to_dict())

    def _build_real_state_locked(self) -> StopState:
        if not self._telemetry_online or self._real_temp is None:
            status = StopStatus.DESCONECTADO
        elif self._real_fan:
            status = StopStatus.VENTILANDO
        else:
            status = StopStatus.NORMAL
        return StopState(
            stop_id=self._real_cfg.stop_id,
            name=self._real_cfg.name,
            origin=self._real_cfg.origin,
            derivation_factor=self._real_cfg.derivation_factor,
            temperature=self._real_temp,
            temp_limit=self._real_limit,
            ventilation=self._real_fan if self._telemetry_online else False,
            mode=self._real_mode,
            status=status,
            last_updated=self._real_last_updated,
        )

    def _build_all_states_locked(self) -> list[StopState]:
        state_map: dict[str, StopState] = {
            self._real_cfg.stop_id: self._build_real_state_locked()
        }
        for virtual_state in self._deriver.get_all_states(connected=self._telemetry_online):
            state_map[virtual_state.stop_id] = virtual_state
        return [state_map[stop_id] for stop_id in self._ordered_ids]

    def _build_summary_locked(self) -> NetworkSummary:
        states = self._build_all_states_locked()
        temps = [s.temperature for s in states if s.temperature is not None]
        avg_temp = round(sum(temps) / len(temps), 2) if temps else None
        max_temp = round(max(temps), 2) if temps else None
        ventilating_count = sum(1 for s in states if s.status == StopStatus.VENTILANDO)
        return NetworkSummary(
            connected=self._telemetry_online,
            operation_mode=self._transport.mode_name,
            average_temperature=avg_temp,
            max_temperature=max_temp,
            ventilating_count=ventilating_count,
            total_stops=len(states),
        )
