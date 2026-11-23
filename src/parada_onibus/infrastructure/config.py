"""Carregamento de configuração via config.toml (tomllib) e variáveis de ambiente."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from parada_onibus.domain.models import AppConfig, StopConfig, StopOrigin
from parada_onibus.domain.parser import validate_temperature_limit


def default_stops() -> tuple[StopConfig, ...]:
    """Retorna a configuração padrão de 3 paradas (1 real + 2 simuladas derivadas)."""
    return (
        StopConfig(
            stop_id="parada-1",
            name="Parada 1 — Maquete (Av. Central)",
            origin=StopOrigin.REAL,
            derivation_factor=1.0,
            initial_limit=30.0,
        ),
        StopConfig(
            stop_id="parada-2",
            name="Parada 2 — Praça Sol Nascente",
            origin=StopOrigin.SIMULADA,
            derivation_factor=1.05,
            initial_limit=30.0,
        ),
        StopConfig(
            stop_id="parada-3",
            name="Parada 3 — Terminal Sombra Verde",
            origin=StopOrigin.SIMULADA,
            derivation_factor=0.95,
            initial_limit=30.0,
        ),
    )


def _parse_stops(raw_stops: list[dict[str, Any]]) -> tuple[StopConfig, ...]:
    parsed: list[StopConfig] = []
    seen_ids: set[str] = set()
    real_count = 0

    for item in raw_stops:
        stop_id = str(item["stop_id"]).strip()
        if not stop_id:
            raise ValueError("stop_id não pode ser vazio.")
        if stop_id in seen_ids:
            raise ValueError(f"stop_id duplicado na configuração: {stop_id}")
        seen_ids.add(stop_id)

        name = str(item["name"]).strip()
        origin = StopOrigin(str(item["origin"]).strip().upper())
        if origin == StopOrigin.REAL:
            real_count += 1

        factor = float(item.get("derivation_factor", 1.0))
        if factor <= 0.0:
            raise ValueError(f"derivation_factor deve ser positivo em {stop_id}: {factor}")

        initial_limit = validate_temperature_limit(float(item.get("initial_limit", 30.0)))
        parsed.append(
            StopConfig(
                stop_id=stop_id,
                name=name,
                origin=origin,
                derivation_factor=factor,
                initial_limit=initial_limit,
            )
        )

    if real_count != 1:
        raise ValueError(
            "A configuração deve conter exatamente 1 parada com origin='REAL' "
            f"(encontradas: {real_count})."
        )
    return tuple(parsed)


def load_config(
    config_path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Carrega AppConfig de arquivo TOML (se existir) e sobrescreve com variáveis PARADA_*."""
    environ = env if env is not None else os.environ
    resolved_path = config_path
    if resolved_path is None:
        env_path = environ.get("PARADA_CONFIG")
        if env_path:
            resolved_path = Path(env_path)
        elif Path("config.toml").is_file():
            resolved_path = Path("config.toml")

    raw: dict[str, Any] = {}
    if resolved_path is not None and resolved_path.is_file():
        raw = tomllib.loads(resolved_path.read_text(encoding="utf-8"))

    server_cfg = raw.get("server", {})
    serial_cfg = raw.get("serial", {})
    storage_cfg = raw.get("storage", {})
    simulator_cfg = raw.get("simulator", {})

    host = str(environ.get("PARADA_HOST", server_cfg.get("host", "0.0.0.0")))
    port = int(environ.get("PARADA_PORT", server_cfg.get("port", 5000)))
    mode = str(environ.get("PARADA_MODE", serial_cfg.get("mode", "serial"))).strip().lower()
    if mode not in ("serial", "simulator"):
        raise ValueError(f"Modo inválido: {mode!r}. Use 'serial' ou 'simulator'.")

    serial_port = str(environ.get("PARADA_SERIAL_PORT", serial_cfg.get("port", "/dev/ttyACM0")))
    baud_rate = int(environ.get("PARADA_BAUD_RATE", serial_cfg.get("baud_rate", 9600)))
    timeout_seconds = float(
        environ.get("PARADA_SERIAL_TIMEOUT", serial_cfg.get("timeout_seconds", 6.0))
    )
    reconnect_max = float(
        environ.get("PARADA_RECONNECT_MAX", serial_cfg.get("reconnect_max_seconds", 5.0))
    )
    db_path = str(
        environ.get("PARADA_DB_PATH", storage_cfg.get("db_path", "data/parada_onibus.db"))
    )
    persist_interval = float(
        environ.get(
            "PARADA_PERSIST_INTERVAL",
            storage_cfg.get("persist_interval_seconds", 1.0),
        )
    )
    chart_points = int(
        environ.get("PARADA_CHART_POINTS", storage_cfg.get("chart_history_points", 120))
    )
    simulator_interval = float(
        environ.get(
            "PARADA_SIMULATOR_INTERVAL",
            simulator_cfg.get("interval_seconds", 1.0),
        )
    )

    raw_stops = raw.get("stops")
    stops = (
        _parse_stops(raw_stops)
        if isinstance(raw_stops, list) and raw_stops
        else default_stops()
    )

    return AppConfig(
        host=host,
        port=port,
        mode=mode,
        serial_port=serial_port,
        baud_rate=baud_rate,
        serial_timeout_seconds=timeout_seconds,
        serial_reconnect_max_seconds=reconnect_max,
        db_path=db_path,
        db_persist_interval_seconds=persist_interval,
        chart_history_points=chart_points,
        simulator_interval_seconds=simulator_interval,
        stops=stops,
    )
