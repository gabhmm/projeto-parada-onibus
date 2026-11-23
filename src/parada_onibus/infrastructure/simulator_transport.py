"""Transporte Simulado Completo (sem hardware) imitando o Arduino autônomo da Parada 1."""

from __future__ import annotations

import json
import logging
import math
import threading

from parada_onibus.domain.models import ArduinoCommand, CommandType, ControlMode
from parada_onibus.domain.ports import LineCallback, StatusCallback

logger = logging.getLogger(__name__)


class SimulatedTransport:
    """Gera telemetria NDJSON sintética cruzando 30 °C e responde a comandos de controle."""

    def __init__(
        self,
        interval_seconds: float = 1.0,
        initial_limit: float = 30.0,
        base_temp: float = 30.2,
        amplitude: float = 3.8,
        period_steps: int = 36,
    ) -> None:
        self._interval = max(0.02, interval_seconds)
        self._base_temp = base_temp
        self._amplitude = amplitude
        self._period_steps = max(4, period_steps)
        self._temp_limit = initial_limit
        self._mode = ControlMode.AUTO
        self._manual_fan = False
        self._step = 0
        self._last_temp = base_temp

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._on_line: LineCallback | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode_name(self) -> str:
        return "simulator"

    def start(self, on_line: LineCallback, on_status: StatusCallback) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._on_line = on_line
        self._stop_event.clear()
        self._connected = True
        on_status(True, "Modo Simulador Completo ativo (sem Arduino físico).")
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(on_line,),
            name="simulator-transport",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._connected = False

    def send_command(self, command: ArduinoCommand) -> bool:
        if not self._connected:
            return False
        with self._lock:
            if command.cmd == CommandType.SET_LIMIT:
                self._temp_limit = round(float(command.value), 2)
            elif command.cmd == CommandType.SET_FAN:
                self._mode = ControlMode.MANUAL
                self._manual_fan = bool(command.value)
            elif command.cmd == CommandType.SET_MODE:
                self._mode = ControlMode(str(command.value).upper())
            frame = self._build_frame_locked(self._last_temp)

        logger.info("Simulador aplicou comando %s=%r", command.cmd.value, command.value)
        if self._on_line is not None:
            self._on_line(frame)
        return True

    def _run_loop(self, on_line: LineCallback) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                angle = (2.0 * math.pi * self._step) / self._period_steps
                ripple = 0.25 * math.sin(angle * 3.0)
                temp = round(self._base_temp + self._amplitude * math.sin(angle) + ripple, 2)
                self._last_temp = temp
                self._step += 1
                frame = self._build_frame_locked(temp)
            on_line(frame)
            self._stop_event.wait(self._interval)

    def _build_frame_locked(self, temp: float) -> str:
        fan = self._manual_fan if self._mode == ControlMode.MANUAL else temp >= self._temp_limit
        payload = {
            "temp": temp,
            "fan": fan,
            "limit": self._temp_limit,
            "mode": self._mode.value,
        }
        return json.dumps(payload, separators=(",", ":"))
