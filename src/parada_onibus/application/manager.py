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

# Ressincronização Raspi -> Arduino (o Arduino reinicia com limite 30.0/AUTO a cada
# abertura da porta Serial por causa do reset via DTR).
RESYNC_RETRY_SECONDS = 3.0
RESYNC_MAX_ATTEMPTS = 3
# Janela após um comando em que a telemetria ainda pode refletir o estado antigo.
COMMAND_GRACE_SECONDS = 2.5
LIMIT_TOLERANCE = 0.05


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
        self._real_sensor_fault: bool = False
        self._real_sensor_error: str | None = None

        # Estado desejado da Parada 1 (fonte da verdade da Raspi, persistido no SQLite).
        self._desired_limit: float = self._real_limit
        self._desired_mode: ControlMode = self._real_mode
        self._desired_manual_fan: bool = real_saved.manual_fan if real_saved else False
        self._resync_pending: bool = False
        self._resync_attempts: int = 0
        self._resync_last_monotonic: float = 0.0
        self._last_command_monotonic: float = float("-inf")

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
            self._handle_ack(parsed, now)
            return

        self._apply_telemetry(parsed, now)

    def _handle_ack(self, ack: ArduinoAck, now: datetime) -> None:
        if ack.ok:
            logger.info("ACK recebido do Arduino: cmd=%s ok=True", ack.cmd)
            return
        logger.warning("Arduino REJEITOU o comando %s.", ack.cmd)
        self._record_and_publish_event(
            SystemEvent(
                timestamp=now,
                stop_id=self._real_cfg.stop_id,
                event_type=EventType.COMMAND_REJECTED,
                message=f"{self._real_cfg.name}: Arduino rejeitou o comando '{ack.cmd}'.",
                payload_json=json.dumps({"ack": ack.cmd, "ok": False}),
            )
        )

    def _apply_telemetry(self, telemetry: ArduinoTelemetry, now: datetime) -> None:
        events_to_emit: list[SystemEvent] = []
        transition_detected = False
        commands_to_send: list[ArduinoCommand] = []
        persist_desired = False

        with self._lock:
            was_online = self._telemetry_online
            was_fault = self._real_sensor_fault
            self._telemetry_online = True
            mono_now = time.monotonic()
            self._last_telemetry_monotonic = mono_now

            if not was_online:
                # Toda (re)conexão: o Arduino pode ter reiniciado com valores padrão.
                self._resync_pending = True
                self._resync_attempts = 0
                self._resync_last_monotonic = 0.0
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
            self._real_sensor_fault = telemetry.sensor_fault
            self._real_sensor_error = telemetry.sensor_error
            if telemetry.limit is not None:
                self._real_limit = telemetry.limit
            if telemetry.mode is not None:
                self._real_mode = telemetry.mode
            self._real_last_updated = now

            if telemetry.sensor_fault and not was_fault:
                events_to_emit.append(
                    SystemEvent(
                        timestamp=now,
                        stop_id=self._real_cfg.stop_id,
                        event_type=EventType.SENSOR_FAULT,
                        message=(
                            f"{self._real_cfg.name}: falha no sensor de temperatura "
                            f"({telemetry.sensor_error}). Arduino em modo de segurança; "
                            "derivação das paradas simuladas pausada."
                        ),
                    )
                )
            elif was_fault and not telemetry.sensor_fault:
                events_to_emit.append(
                    SystemEvent(
                        timestamp=now,
                        stop_id=self._real_cfg.stop_id,
                        event_type=EventType.SENSOR_RECOVERED,
                        message=f"{self._real_cfg.name}: sensor de temperatura normalizado.",
                    )
                )

            if telemetry.temp is not None:
                real_reading = StopReading(
                    timestamp=now,
                    stop_id=self._real_cfg.stop_id,
                    stop_name=self._real_cfg.name,
                    origin=self._real_cfg.origin,
                    temperature=telemetry.temp,
                    temp_limit=self._real_limit,
                    ventilation=self._real_fan,
                    mode=self._real_mode,
                )
                virtual_readings = self._deriver.derive_from_real(telemetry.temp, now)
                all_readings = [real_reading, *virtual_readings]
            else:
                # Sem temperatura não há leitura para gravar (coluna NOT NULL).
                self._deriver.apply_sensor_fault(now)
                all_readings = []

            states = self._build_all_states_locked()
            for st in states:
                prev = self._prev_ventilation.get(st.stop_id)
                if prev is not None and prev != st.ventilation:
                    transition_detected = True
                    ev_type = (
                        EventType.VENTILATION_ON if st.ventilation else EventType.VENTILATION_OFF
                    )
                    action_txt = "ligada" if st.ventilation else "desligada"
                    detail = (
                        f"{st.temperature:.2f} °C / limite {st.temp_limit:.1f} °C"
                        if st.temperature is not None
                        else f"falha no sensor / limite {st.temp_limit:.1f} °C"
                    )
                    events_to_emit.append(
                        SystemEvent(
                            timestamp=now,
                            stop_id=st.stop_id,
                            event_type=ev_type,
                            message=f"{st.name}: ventilação {action_txt} ({detail}).",
                        )
                    )
                self._prev_ventilation[st.stop_id] = st.ventilation

            commands_to_send, resync_events, persist_desired = self._reconcile_locked(
                telemetry, now, mono_now
            )
            events_to_emit.extend(resync_events)

            should_persist = bool(all_readings) and (
                transition_detected
                or not was_online
                or (mono_now - self._last_persist_monotonic)
                >= self._config.db_persist_interval_seconds
            )
            if should_persist:
                self._last_persist_monotonic = mono_now

            summary = self._build_summary_locked()
            states = self._build_all_states_locked()
            desired = (self._desired_limit, self._desired_mode, self._desired_manual_fan)

        if should_persist:
            try:
                self._repo.save_readings(all_readings)
            except Exception:
                logger.exception("Erro ao persistir leituras no SQLite.")

        if persist_desired:
            try:
                self._repo.save_stop_setting(self._real_cfg.stop_id, *desired)
            except Exception:
                logger.exception("Erro ao persistir configuração da Parada 1.")

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

        # Enviado por último e fora do lock: o simulador responde de forma síncrona.
        for cmd in commands_to_send:
            if not self._transport.send_command(cmd):
                logger.warning("Falha ao enviar comando de ressincronização %s.", cmd.cmd.value)

    def _reconcile_locked(
        self,
        telemetry: ArduinoTelemetry,
        now: datetime,
        mono_now: float,
    ) -> tuple[list[ArduinoCommand], list[SystemEvent], bool]:
        """Compara o estado reportado pelo Arduino com o estado desejado da Raspi.

        - Após (re)conexão: reenvia limite/modo salvos até o Arduino confirmar.
        - Online: mudanças de modo/cooler pelo botão físico viram o novo estado desejado;
          divergência de limite (o Arduino não altera o limite sozinho) indica reinício
          do Arduino e dispara nova ressincronização.
        """
        events: list[SystemEvent] = []
        in_grace = (mono_now - self._last_command_monotonic) < COMMAND_GRACE_SECONDS
        limit_diverges = (
            telemetry.limit is not None
            and abs(telemetry.limit - self._desired_limit) > LIMIT_TOLERANCE
        )

        if not self._resync_pending:
            if in_grace:
                return [], events, False
            if limit_diverges:
                logger.warning(
                    "Limite do Arduino (%.1f) difere do salvo (%.1f): provável reinício.",
                    telemetry.limit,
                    self._desired_limit,
                )
                self._resync_pending = True
                self._resync_attempts = 0
                self._resync_last_monotonic = 0.0
            else:
                return [], events, self._adopt_local_changes_locked(telemetry)

        commands = self._resync_commands_locked(telemetry)
        if not commands:
            if self._resync_attempts > 0:
                events.append(
                    SystemEvent(
                        timestamp=now,
                        stop_id=self._real_cfg.stop_id,
                        event_type=EventType.STATE_RESYNC,
                        message=(
                            f"{self._real_cfg.name}: configuração salva restaurada no Arduino "
                            f"(limite {self._desired_limit:.1f} °C, modo "
                            f"{self._desired_mode.value})."
                        ),
                    )
                )
            self._resync_pending = False
            return [], events, False

        if self._resync_attempts >= RESYNC_MAX_ATTEMPTS:
            if (mono_now - self._resync_last_monotonic) < RESYNC_RETRY_SECONDS:
                return [], events, False
            # Desiste e adota o estado do Arduino para não entrar em laço.
            self._resync_pending = False
            if telemetry.limit is not None:
                self._desired_limit = telemetry.limit
            self._adopt_local_changes_locked(telemetry)
            events.append(
                SystemEvent(
                    timestamp=now,
                    stop_id=self._real_cfg.stop_id,
                    event_type=EventType.STATE_RESYNC,
                    message=(
                        f"{self._real_cfg.name}: Arduino não confirmou a configuração salva "
                        f"após {RESYNC_MAX_ATTEMPTS} tentativas; adotando os valores do Arduino."
                    ),
                )
            )
            return [], events, True

        if (
            self._resync_attempts > 0
            and (mono_now - self._resync_last_monotonic) < RESYNC_RETRY_SECONDS
        ):
            return [], events, False

        self._resync_attempts += 1
        self._resync_last_monotonic = mono_now
        self._last_command_monotonic = mono_now
        logger.info(
            "Ressincronizando Arduino (tentativa %d/%d): %s",
            self._resync_attempts,
            RESYNC_MAX_ATTEMPTS,
            [(c.cmd.value, c.value) for c in commands],
        )
        return commands, events, False

    def _resync_commands_locked(self, telemetry: ArduinoTelemetry) -> list[ArduinoCommand]:
        commands: list[ArduinoCommand] = []
        if (
            telemetry.limit is not None
            and abs(telemetry.limit - self._desired_limit) > LIMIT_TOLERANCE
        ):
            commands.append(ArduinoCommand(cmd=CommandType.SET_LIMIT, value=self._desired_limit))
        if telemetry.mode is not None:
            if self._desired_mode == ControlMode.AUTO and telemetry.mode == ControlMode.MANUAL:
                commands.append(
                    ArduinoCommand(cmd=CommandType.SET_MODE, value=ControlMode.AUTO.value)
                )
            elif self._desired_mode == ControlMode.MANUAL and (
                telemetry.mode != ControlMode.MANUAL or telemetry.fan != self._desired_manual_fan
            ):
                commands.append(
                    ArduinoCommand(cmd=CommandType.SET_FAN, value=self._desired_manual_fan)
                )
        return commands

    def _adopt_local_changes_locked(self, telemetry: ArduinoTelemetry) -> bool:
        """Aceita mudanças feitas no próprio Arduino (botão físico). Retorna se mudou algo."""
        if telemetry.mode is None:
            return False
        new_fan = telemetry.fan if telemetry.mode == ControlMode.MANUAL else False
        if (telemetry.mode, new_fan) == (self._desired_mode, self._desired_manual_fan):
            return False
        logger.info(
            "Estado alterado localmente no Arduino: modo=%s cooler=%s",
            telemetry.mode.value,
            telemetry.fan,
        )
        self._desired_mode = telemetry.mode
        self._desired_manual_fan = new_fan
        return True

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
            with self._lock:
                # Marcado antes do envio: o simulador responde com telemetria síncrona.
                previous_limit = self._desired_limit
                self._desired_limit = validated
                self._last_command_monotonic = time.monotonic()
            if not self._transport.send_command(cmd):
                with self._lock:
                    self._desired_limit = previous_limit
                return CommandResult(
                    ok=False,
                    stop_id=stop_id,
                    message="Falha ao gravar comando na porta Serial.",
                    http_status=503,
                )
            with self._lock:
                self._real_limit = validated
                self._desired_limit = validated
                self._last_command_monotonic = time.monotonic()
                mode = self._desired_mode
                manual_fan = self._desired_manual_fan
                state = self._build_real_state_locked()
            self._repo.save_stop_setting(stop_id, validated, mode, manual_fan)
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
            with self._lock:
                # Marcado antes do envio: o simulador responde com telemetria síncrona.
                self._last_command_monotonic = time.monotonic()
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
                    self._desired_mode = ControlMode.MANUAL
                    self._desired_manual_fan = False
                else:
                    self._real_mode = ControlMode.AUTO
                    self._desired_mode = ControlMode.AUTO
                    self._desired_manual_fan = False
                self._last_command_monotonic = time.monotonic()
                limit = self._desired_limit
                mode = self._desired_mode
                manual_fan = self._desired_manual_fan
                state = self._build_real_state_locked()
            self._repo.save_stop_setting(stop_id, limit, mode, manual_fan)
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
        if not self._telemetry_online:
            status = StopStatus.DESCONECTADO
        elif self._real_sensor_fault:
            status = StopStatus.FALHA_SENSOR
        elif self._real_temp is None:
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
        ventilating_count = sum(
            1
            for s in states
            if s.status == StopStatus.VENTILANDO
            or (s.status == StopStatus.FALHA_SENSOR and s.ventilation)
        )
        return NetworkSummary(
            connected=self._telemetry_online,
            operation_mode=self._transport.mode_name,
            average_temperature=avg_temp,
            max_temperature=max_temp,
            ventilating_count=ventilating_count,
            total_stops=len(states),
            sensor_fault=self._telemetry_online and self._real_sensor_fault,
            sensor_error=self._real_sensor_error if self._telemetry_online else None,
        )
