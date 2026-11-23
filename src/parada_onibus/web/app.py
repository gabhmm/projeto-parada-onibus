"""Factory da aplicação FastAPI com gerenciamento de ciclo de vida (lifespan)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from parada_onibus import __version__
from parada_onibus.application.broadcaster import SSEBroadcaster
from parada_onibus.application.manager import StopNetworkManager
from parada_onibus.domain.models import AppConfig, StopOrigin
from parada_onibus.domain.ports import DataRepository, TelemetryTransport
from parada_onibus.infrastructure.config import load_config
from parada_onibus.infrastructure.serial_transport import SerialTransport
from parada_onibus.infrastructure.simulator_transport import SimulatedTransport
from parada_onibus.infrastructure.sqlite_repository import SQLiteRepository
from parada_onibus.web.routes import STATIC_DIR, build_router


def _build_default_transport(config: AppConfig, initial_limit: float) -> TelemetryTransport:
    if config.mode == "simulator":
        return SimulatedTransport(
            interval_seconds=config.simulator_interval_seconds,
            initial_limit=initial_limit,
        )
    return SerialTransport(
        port=config.serial_port,
        baud_rate=config.baud_rate,
        reconnect_max_seconds=config.serial_reconnect_max_seconds,
    )


def create_app(
    config: AppConfig | None = None,
    *,
    transport: TelemetryTransport | None = None,
    repository: DataRepository | None = None,
) -> FastAPI:
    """Constrói e conecta todas as camadas da aplicação FastAPI."""
    resolved_config = config if config is not None else load_config()
    repo: DataRepository = (
        repository if repository is not None else SQLiteRepository(resolved_config.db_path)
    )
    repo.initialize()

    real_stop = next(s for s in resolved_config.stops if s.origin == StopOrigin.REAL)
    saved_settings = repo.load_stop_settings()
    saved_real = saved_settings.get(real_stop.stop_id)
    initial_real_limit = saved_real.temp_limit if saved_real else real_stop.initial_limit

    resolved_transport: TelemetryTransport = (
        transport
        if transport is not None
        else _build_default_transport(resolved_config, initial_real_limit)
    )
    broadcaster = SSEBroadcaster()
    manager = StopNetworkManager(
        config=resolved_config,
        transport=resolved_transport,
        repository=repo,
        broadcaster=broadcaster,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()
            repo.close()

    app = FastAPI(
        title="Monitoramento de Paradas de Ônibus — Raspberry Pi",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.state.config = resolved_config

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(build_router())
    return app
