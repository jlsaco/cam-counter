"""Rutas REST ``/api/*`` + endpoint WebSocket ``/api/ws``.

Same-origin (sin CORS, sin auth de nube). Las ESCRITURAS (PUT config, reset de
counters) pasan por un gate OPCIONAL de token compartido leído de
``CAMCOUNTER_API_TOKEN`` (si no está configurado, se permiten — confianza LAN).

Mapeo de errores a HTTP:
- slug de ``camera_id`` malformado -> ``400``
- cámara válida pero desconocida   -> ``404``
- ``config_version`` desactualizado en PUT -> ``409``
- token requerido y ausente/incorrecto     -> ``401``
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Annotated

from cam_counter_edge import InvalidSlugError, StaleConfigVersionError
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

import mjpeg
from engine import Engine, UnknownCameraError
from schemas import (
    Camera,
    Counters,
    CrossingEvent,
    DeviceInfo,
    Health,
    LineConfig,
    LineConfigUpdate,
)
from settings import get_settings

router = APIRouter()


def get_engine(request: Request) -> Engine:
    """Recupera el ``Engine`` montado en ``app.state`` (inyección de dependencia)."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:  # pragma: no cover - sólo si se llama sin lifespan
        raise HTTPException(status_code=503, detail="engine no inicializado")
    return engine


def require_write_auth(
    x_api_token: Annotated[str | None, Header(alias="X-API-Token")] = None,
) -> None:
    """Gate OPCIONAL de token para escrituras (env ``CAMCOUNTER_API_TOKEN``).

    Sin token configurado en el entorno: se permite (confianza LAN). Con token
    configurado: exige que la cabecera ``X-API-Token`` coincida, o devuelve 401.
    """
    expected = get_settings().api_token
    if expected is None:
        return
    if x_api_token != expected:
        raise HTTPException(status_code=401, detail="token de escritura inválido o ausente")


def _resolve_camera(engine: Engine, camera_id: str) -> str:
    """Valida slug + pertenencia; traduce a 400/404."""
    try:
        return engine.require_known_camera(camera_id)
    except InvalidSlugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnknownCameraError as exc:
        raise HTTPException(status_code=404, detail=f"cámara desconocida: {camera_id}") from exc


EngineDep = Annotated[Engine, Depends(get_engine)]


# -- device / health ------------------------------------------------------- #


@router.get("/device", response_model=DeviceInfo, tags=["device"])
async def get_device(engine: EngineDep) -> DeviceInfo:
    return await engine.get_device_info()


@router.get("/health", response_model=Health, tags=["health"])
async def get_health(engine: EngineDep) -> Health:
    return await engine.get_health()


# -- cámaras --------------------------------------------------------------- #


@router.get("/cameras", response_model=list[Camera], tags=["cameras"])
async def list_cameras(engine: EngineDep) -> list[Camera]:
    return await engine.list_cameras()


@router.get("/cameras/{camera_id}", response_model=Camera, tags=["cameras"])
async def get_camera(engine: EngineDep, camera_id: str) -> Camera:
    _resolve_camera(engine, camera_id)
    return await engine.get_camera(camera_id)


# -- config de línea (hot-reload) ----------------------------------------- #


@router.get("/cameras/{camera_id}/config", response_model=LineConfig, tags=["config"])
async def get_config(engine: EngineDep, camera_id: str) -> LineConfig:
    _resolve_camera(engine, camera_id)
    return await engine.get_line_config(camera_id)


@router.put(
    "/cameras/{camera_id}/config",
    response_model=LineConfig,
    tags=["config"],
    dependencies=[Depends(require_write_auth)],
)
async def put_config(
    engine: EngineDep, camera_id: str, update: LineConfigUpdate
) -> LineConfig:
    _resolve_camera(engine, camera_id)
    try:
        return await engine.put_line_config(camera_id, update)
    except StaleConfigVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "config_version desactualizado; recarga y reintenta",
                "expected": exc.expected,
                "current": exc.current,
            },
        ) from exc


# -- counters -------------------------------------------------------------- #


@router.get("/cameras/{camera_id}/counters", response_model=Counters, tags=["counters"])
async def get_counters(engine: EngineDep, camera_id: str) -> Counters:
    _resolve_camera(engine, camera_id)
    return await engine.get_counters(camera_id)


@router.post(
    "/cameras/{camera_id}/counters/reset",
    response_model=Counters,
    tags=["counters"],
    dependencies=[Depends(require_write_auth)],
)
async def reset_counters(engine: EngineDep, camera_id: str) -> Counters:
    _resolve_camera(engine, camera_id)
    return await engine.reset_counters(camera_id)


# -- histórico paginado ---------------------------------------------------- #


@router.get(
    "/cameras/{camera_id}/events", response_model=list[CrossingEvent], tags=["events"]
)
async def get_events(
    engine: EngineDep,
    camera_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CrossingEvent]:
    _resolve_camera(engine, camera_id)
    return await engine.get_events(camera_id, limit=limit, offset=offset)


# -- MJPEG (primitivo de vídeo en vivo) ----------------------------------- #


@router.get("/cameras/{camera_id}/stream.mjpg", tags=["stream"])
async def stream_mjpeg(
    engine: EngineDep,
    camera_id: str,
    frames: Annotated[int | None, Query(ge=1, le=10000)] = None,
) -> StreamingResponse:
    """Stream MJPEG (``multipart/x-mixed-replace``) de la cámara.

    Sin ``frames`` el stream es indefinido (hasta que el cliente se desconecte);
    con ``frames=N`` emite N frames y termina (útil para tests/E2E acotados).
    """
    _resolve_camera(engine, camera_id)
    interval = engine.frame_interval_s

    async def generator() -> AsyncGenerator[bytes, None]:
        emitted = 0
        while frames is None or emitted < frames:
            yield mjpeg.multipart_chunk(engine.get_frame(camera_id))
            emitted += 1
            if frames is None or emitted < frames:
                await asyncio.sleep(interval)

    return StreamingResponse(
        generator(),
        media_type=f"multipart/x-mixed-replace; boundary={mjpeg.MULTIPART_BOUNDARY}",
        headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
    )


# -- WebSocket hub --------------------------------------------------------- #


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Hub WS: difunde envelopes (counter_update/camera_status/config_changed/crossing).

    El servidor es push-only; los mensajes entrantes del cliente se ignoran (sólo
    se usan para detectar la desconexión).
    """
    hub = websocket.app.state.hub
    await hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(websocket)
