"""API + UI local (FastAPI) — harness MÍNIMO para E2E de PR10.

⚠️ ALCANCE: PR09 (FastAPI completo + SPA React/Vite/Tailwind + fuente fake) NO
está presente en esta base de la pila. Este módulo es un harness MÍNIMO,
self-contained y *same-origin* (sin CORS), suficiente para una suite Playwright
E2E REAL del flujo de la línea de conteo. Cuando PR09 aterrice, su app la
sustituye; aquí sólo se implementa lo imprescindible para el E2E:

- ``GET /api/line`` / ``PUT /api/line``: config de la línea por cámara, persistida
  en LOCAL (fichero JSON) con ``config_version`` MONÓTONO (hot-reload sin
  reiniciar). Espejo del contrato ``contracts/line_config.schema.json``.
- ``GET /api/counters``: contadores ``in``/``out`` en vivo.
- ``WS /api/ws``: empuja los contadores al cambiar (incremento en vivo).
- ``GET /api/stream``: MJPEG fake (multipart/x-mixed-replace) en bucle.
- ``GET /``: sirve la SPA estática (``v1/ui/public/index.html``), same-origin.

FUENTE FAKE (env-gated ``CAMCOUNTER_FAKE_SOURCE=1``): un bucle de fondo emite
cruces GUIONIZADOS deterministas que incrementan los contadores y los empujan por
WS, para un E2E reproducible y dev local SIN Pi ni cámara.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# PNG 1x1 (placeholder del "frame" del MJPEG fake; el navegador lo renderiza).
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAAAAAEAAH2FzhV" "AAAAAElFTkSuQmCC"
)

_UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "public"
_STATE_PATH = Path(os.environ.get("CAMCOUNTER_LINE_STATE", "/tmp/cam_counter_line.json"))

# Identidad de la cámara fake (slugs válidos, forma {device_id}-cam{N}).
_SITE, _DEVICE, _CAMERA = "sitio-demo", "rpi-001", "rpi-001-cam0"


class Point(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class Line(BaseModel):
    a: Point
    b: Point


class LineConfig(BaseModel):
    """Espejo del contrato ``line_config.schema.json`` (subset usado por el E2E)."""

    site_id: str = _SITE
    device_id: str = _DEVICE
    camera_id: str = _CAMERA
    config_version: int = 1
    line: Line
    positive_side: int = 1
    positive_label: str | None = "subieron"
    negative_label: str | None = "bajaron"
    updated_at: str | None = None
    schema_version: int = 1


def _default_config() -> LineConfig:
    return LineConfig(line=Line(a=Point(x=0.5, y=0.1), b=Point(x=0.5, y=0.9)))


def _load_config() -> LineConfig:
    if _STATE_PATH.exists():
        try:
            return LineConfig(**json.loads(_STATE_PATH.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 (estado corrupto => default)
            pass
    return _default_config()


def _save_config(cfg: LineConfig) -> None:
    _STATE_PATH.write_text(cfg.model_dump_json(), encoding="utf-8")


class _State:
    """Estado en memoria del harness (config de línea + contadores + clientes WS)."""

    def __init__(self) -> None:
        self.config: LineConfig = _load_config()
        self.counters: dict[str, int] = {"in": 0, "out": 0}
        self.ws_clients: set[WebSocket] = set()

    async def broadcast(self) -> None:
        payload = {"type": "counters", "counters": dict(self.counters)}
        dead: list[WebSocket] = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 (cliente caído => purgar)
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _fake_source_loop(state: _State) -> None:
    """Emite cruces GUIONIZADOS deterministas (incrementa contadores + WS)."""
    directions = ["in", "out", "in", "in", "out"]
    i = 0
    while True:
        await asyncio.sleep(float(os.environ.get("CAMCOUNTER_FAKE_INTERVAL", "0.5")))
        direction = directions[i % len(directions)]
        i += 1
        state.counters[direction] += 1
        await state.broadcast()


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    state: _State = app.state.cc  # type: ignore[attr-defined]
    task: asyncio.Task[None] | None = None
    if os.environ.get("CAMCOUNTER_FAKE_SOURCE") == "1":
        task = asyncio.create_task(_fake_source_loop(state))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="cam-counter local API (PR10 harness)", lifespan=lifespan)
    app.state.cc = _State()  # type: ignore[attr-defined]

    def state() -> _State:
        return app.state.cc  # type: ignore[attr-defined,no-any-return]

    @app.get("/api/line")
    def get_line() -> Any:
        return state().config.model_dump()

    @app.put("/api/line")
    def put_line(cfg: LineConfig) -> Any:
        st = state()
        # config_version MONÓTONO: nunca decrece (hot-reload sin reinicio).
        cfg.config_version = max(cfg.config_version, st.config.config_version + 1)
        cfg.updated_at = _now_iso()
        st.config = cfg
        _save_config(cfg)
        return cfg.model_dump()

    @app.get("/api/counters")
    def get_counters() -> Any:
        return {"counters": state().counters}

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        st = state()
        st.ws_clients.add(websocket)
        # Empuja el estado actual al conectar (snapshot inicial).
        await websocket.send_json({"type": "counters", "counters": dict(st.counters)})
        try:
            while True:
                await websocket.receive_text()  # mantiene viva la conexión
        except WebSocketDisconnect:
            pass
        finally:
            st.ws_clients.discard(websocket)

    @app.get("/api/stream")
    def stream() -> StreamingResponse:
        boundary = "frame"

        def gen() -> Any:
            for _ in range(120):  # bucle acotado de frames fake
                yield (
                    f"--{boundary}\r\nContent-Type: image/png\r\n"
                    f"Content-Length: {len(_PNG_1X1)}\r\n\r\n"
                ).encode() + _PNG_1X1 + b"\r\n"

        return StreamingResponse(
            gen(),
            media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        )

    @app.get("/api/device")
    def device() -> Any:
        return {"site_id": _SITE, "device_id": _DEVICE, "camera_ids": [_CAMERA]}

    @app.get("/")
    def index() -> Any:
        idx = _UI_DIR / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return JSONResponse({"error": "SPA no encontrada"}, status_code=404)

    return app


app = create_app()
