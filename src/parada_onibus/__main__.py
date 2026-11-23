"""Entrypoint CLI: `python -m parada_onibus [--simulator] [--port 5000]`."""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path

import uvicorn

from parada_onibus.infrastructure.config import load_config
from parada_onibus.web.app import create_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parada-onibus",
        description="Dashboard central de monitoramento e controle de paradas de ônibus.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Caminho para o arquivo config.toml (opcional).",
    )
    parser.add_argument(
        "--simulator",
        action="store_true",
        help="Executa no Modo Simulador Completo (sem Arduino conectado).",
    )
    parser.add_argument("--host", type=str, default=None, help="Endereço de bind HTTP.")
    parser.add_argument("--port", type=int, default=None, help="Porta HTTP (padrão: 5000).")
    parser.add_argument("--serial-port", type=str, default=None, help="Porta Serial USB.")
    parser.add_argument("--baud-rate", type=int, default=None, help="Baud rate da porta Serial.")
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Nível de logging da aplicação.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config(args.config)
    if args.simulator:
        config = replace(config, mode="simulator")
    if args.host is not None:
        config = replace(config, host=args.host)
    if args.port is not None:
        config = replace(config, port=args.port)
    if args.serial_port is not None:
        config = replace(config, serial_port=args.serial_port)
    if args.baud_rate is not None:
        config = replace(config, baud_rate=args.baud_rate)

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
