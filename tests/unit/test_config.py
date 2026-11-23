"""Testes unitários do carregador de configuração TOML e variáveis de ambiente."""

from __future__ import annotations

from pathlib import Path

import pytest

from parada_onibus.infrastructure.config import load_config


def test_load_default_config_without_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "inexistente.toml", env={})
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 5000
    assert cfg.mode == "serial"
    assert len(cfg.stops) == 3
    assert cfg.stops[0].derivation_factor == 1.0
    assert cfg.stops[1].derivation_factor == 1.05
    assert cfg.stops[2].derivation_factor == 0.95


def test_load_toml_and_env_override(tmp_path: Path) -> None:
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(
        """
[server]
host = "127.0.0.1"
port = 8080

[serial]
mode = "serial"
port = "/dev/ttyUSB0"
baud_rate = 115200
""",
        encoding="utf-8",
    )

    cfg = load_config(
        toml_file,
        env={"PARADA_MODE": "simulator", "PARADA_PORT": "5050"},
    )
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 5050
    assert cfg.mode == "simulator"
    assert cfg.serial_port == "/dev/ttyUSB0"
    assert cfg.baud_rate == 115200


def test_load_config_rejects_invalid_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_config(tmp_path / "none.toml", env={"PARADA_MODE": "invalido"})
