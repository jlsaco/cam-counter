"""Hub WebSocket: difunde ``WsEnvelope`` a todos los clientes conectados.

El motor (``engine.py``) publica eventos (``counter_update``, ``camera_status``,
``config_changed``, ``crossing``) desde hilos de trabajo y desde los handlers
asíncronos; el hub los entrega a las conexiones WS. La publicación es no
bloqueante: ``publish_threadsafe`` agenda la difusión en el event loop sin
bloquear al hilo productor (la fuente falsa / el pipeline de conteo).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from schemas import WsEnvelope

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

__all__ = ["WsHub"]


class WsHub:
    """Registro de conexiones WS + difusión de envelopes.

    Mantiene el event loop de asyncio (capturado al arrancar) para poder publicar
    de forma thread-safe desde hilos productores vía ``run_coroutine_threadsafe``.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Asocia el event loop activo (llamado en el arranque de la app)."""
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        """Acepta y registra una conexión WS."""
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        """Da de baja una conexión WS."""
        async with self._lock:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        """Número de clientes WS conectados (observabilidad/tests)."""
        return len(self._clients)

    async def broadcast(self, envelope: WsEnvelope) -> None:
        """Envía el envelope a todos los clientes; descarta los que fallen."""
        payload = envelope.model_dump()
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 — un cliente caído no tumba al resto
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    def publish_threadsafe(self, envelope: WsEnvelope) -> None:
        """Publica desde CUALQUIER hilo sin bloquear: agenda en el event loop.

        No-op silencioso si el loop aún no está asociado (arranque/cierre).
        """
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            asyncio.run_coroutine_threadsafe(self.broadcast(envelope), loop)
