"""Rotas HTTP REST, exportação CSV em streaming e canal SSE (/api/stream)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from parada_onibus.application.broadcaster import SSEMessage
from parada_onibus.application.manager import StopNetworkManager
from parada_onibus.infrastructure.csv_exporter import iter_csv_chunks

STATIC_DIR = Path(__file__).resolve().parent / "static"


class LimitUpdateRequest(BaseModel):
    """Payload para alterar a temperatura limite de uma parada."""

    limit: float = Field(..., description="Nova temperatura limite em °C (15.0 a 50.0)")


class VentilationCommandRequest(BaseModel):
    """Payload para comando de ventilação (override manual ou retorno ao automático)."""

    action: str = Field(..., description="'turn_off' para desligar manualmente ou 'auto'")


def _get_manager(request: Request) -> StopNetworkManager:
    manager: StopNetworkManager = request.app.state.manager
    return manager


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @router.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        return _get_manager(request).get_health()

    @router.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        return _get_manager(request).get_snapshot()

    @router.get("/api/stops/{stop_id}/readings")
    async def stop_readings(
        request: Request,
        stop_id: str,
        limit: int = Query(default=120, ge=1, le=2000),
    ) -> dict[str, Any]:
        manager = _get_manager(request)
        if not manager.has_stop(stop_id):
            raise HTTPException(status_code=404, detail=f"Parada não encontrada: {stop_id}")
        readings = manager.repository.get_recent_readings(stop_id, limit=limit)
        return {
            "stop_id": stop_id,
            "count": len(readings),
            "readings": [r.to_dict() for r in readings],
        }

    @router.get("/api/events")
    async def recent_events(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        manager = _get_manager(request)
        events = manager.repository.get_recent_events(limit=limit)
        return {
            "count": len(events),
            "events": [e.to_dict() for e in events],
        }

    @router.post("/api/stops/{stop_id}/limit")
    async def update_limit(
        request: Request,
        stop_id: str,
        payload: LimitUpdateRequest,
    ) -> JSONResponse:
        manager = _get_manager(request)
        result = manager.set_stop_limit(stop_id, payload.limit)
        return JSONResponse(content=result.to_dict(), status_code=result.http_status)

    @router.post("/api/stops/{stop_id}/ventilation")
    async def update_ventilation(
        request: Request,
        stop_id: str,
        payload: VentilationCommandRequest,
    ) -> JSONResponse:
        manager = _get_manager(request)
        result = manager.set_stop_ventilation(stop_id, payload.action)
        return JSONResponse(content=result.to_dict(), status_code=result.http_status)

    @router.get("/api/export/csv")
    async def export_csv(
        request: Request,
        stop_id: str | None = Query(default=None),
    ) -> StreamingResponse:
        manager = _get_manager(request)
        if stop_id is not None and not manager.has_stop(stop_id):
            raise HTTPException(status_code=404, detail=f"Parada não encontrada: {stop_id}")

        timestamp_tag = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        prefix = stop_id if stop_id is not None else "paradas_onibus"
        filename = f"{prefix}_{timestamp_tag}.csv"

        stream = iter_csv_chunks(manager.repository.iter_readings_for_export(stop_id=stop_id))
        return StreamingResponse(
            stream,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @router.get("/api/stream")
    async def sse_stream(request: Request) -> StreamingResponse:
        manager = _get_manager(request)

        async def event_generator() -> AsyncIterator[str]:
            queue = manager.broadcaster.subscribe()
            try:
                initial = SSEMessage(event="snapshot", data=manager.get_snapshot())
                yield initial.encode()

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue

                    if item is None:
                        break
                    yield item.encode()
            finally:
                manager.broadcaster.unsubscribe(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
