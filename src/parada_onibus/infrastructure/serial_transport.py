"""Transporte Serial bidirecional (pyserial) com reconexão automática em thread dedicada."""

from __future__ import annotations

import logging
import threading

import serial

from parada_onibus.domain.models import ArduinoCommand
from parada_onibus.domain.parser import serialize_command
from parada_onibus.domain.ports import LineCallback, StatusCallback

logger = logging.getLogger(__name__)


class SerialTransport:
    """Gerencia leitura contínua NDJSON e envio de comandos ao Arduino via USB Serial."""

    def __init__(
        self,
        port: str,
        baud_rate: int = 9600,
        reconnect_max_seconds: float = 5.0,
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._reconnect_max = max(1.0, reconnect_max_seconds)
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode_name(self) -> str:
        return "serial"

    def start(self, on_line: LineCallback, on_status: StatusCallback) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(on_line, on_status),
            name="serial-transport",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_serial()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._connected = False

    def send_command(self, command: ArduinoCommand) -> bool:
        payload = serialize_command(command).encode("utf-8")
        with self._write_lock:
            conn = self._serial
            if conn is None or not conn.is_open:
                logger.warning("Tentativa de envio de comando com Serial desconectada.")
                return False
            try:
                conn.write(payload)
                conn.flush()
                logger.info("Comando enviado via Serial: %s", payload.decode("utf-8").strip())
                return True
            except (serial.SerialException, OSError) as exc:
                logger.error("Erro ao enviar comando na porta Serial %s: %s", self._port, exc)
                return False

    def _run_loop(self, on_line: LineCallback, on_status: StatusCallback) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                logger.info("Conectando à porta Serial %s (%d bps)...", self._port, self._baud_rate)
                conn = serial.Serial(
                    port=self._port,
                    baudrate=self._baud_rate,
                    timeout=1.0,
                    write_timeout=2.0,
                )
                with self._write_lock:
                    self._serial = conn
                self._connected = True
                backoff = 1.0
                on_status(True, f"Porta Serial conectada em {self._port} ({self._baud_rate} bps).")

                while not self._stop_event.is_set():
                    raw = conn.readline()
                    if not raw:
                        continue
                    decoded = raw.decode("utf-8", errors="replace").strip()
                    if decoded:
                        on_line(decoded)

            except (serial.SerialException, OSError) as exc:
                if self._connected:
                    self._connected = False
                    on_status(False, f"Perda de conexão Serial ({self._port}): {exc}")
                else:
                    logger.warning(
                        "Falha ao abrir porta Serial %s (%s). Nova tentativa em %.1fs.",
                        self._port,
                        exc,
                        backoff,
                    )
            finally:
                self._close_serial()
                if self._connected and not self._stop_event.is_set():
                    self._connected = False
                    on_status(False, f"Porta Serial {self._port} encerrada.")

            if not self._stop_event.is_set():
                self._stop_event.wait(backoff)
                backoff = min(self._reconnect_max, backoff * 2.0)

    def _close_serial(self) -> None:
        with self._write_lock:
            conn = self._serial
            self._serial = None
            if conn is not None:
                try:
                    if conn.is_open:
                        conn.close()
                except OSError:
                    pass
