"""Testes do transporte Serial sem hardware (pyserial substituído por um fake)."""

from __future__ import annotations

import threading
import time

import pytest

from parada_onibus.infrastructure import serial_transport as st


class _SilentSerial:
    """Porta que abre normalmente, mas nunca recebe bytes (link USB morto)."""

    opened = 0

    def __init__(self, **_: object) -> None:
        type(self).opened += 1
        self.is_open = True

    def reset_input_buffer(self) -> None:
        pass

    def readline(self) -> bytes:
        time.sleep(0.02)
        return b""

    def close(self) -> None:
        self.is_open = False


def test_reopens_port_after_idle_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    _SilentSerial.opened = 0
    monkeypatch.setattr(st.serial, "Serial", _SilentSerial)

    transport = st.SerialTransport(port="/dev/fake", reconnect_max_seconds=1.0)
    transport._idle_reconnect = 0.1  # encurta o limite mínimo só para o teste

    statuses: list[bool] = []
    two_drops = threading.Event()

    def on_status(connected: bool, _detail: str) -> None:
        statuses.append(connected)
        if statuses.count(False) >= 2:
            two_drops.set()

    transport.start(on_line=lambda _line: None, on_status=on_status)
    try:
        assert two_drops.wait(timeout=6.0), statuses
    finally:
        transport.stop()

    assert _SilentSerial.opened >= 2
    assert statuses[:2] == [True, False]
