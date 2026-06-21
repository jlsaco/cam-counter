"""Ensamblado de la app FastAPI: API ``/api/*`` + SPA same-origin desde el Pi.

Un único proceso Uvicorn sirve:
- la API REST/WS bajo ``/api`` (OpenAPI en ``/api/openapi.json``, docs en
  ``/api/docs``), y
- la SPA React/Vite/Tailwind construida (``v1/ui/dist``) en TODAS las demás rutas,
  con fallback a ``index.html`` (SPA routing). Same-origin: sin CORS, sin auth de
  nube.

Importar este módulo NUNCA arranca la fuente ni requiere ``v1/ui/dist``: el motor
se arranca en el ``lifespan`` (sólo al servir) y la ausencia de ``dist`` degrada a
un placeholder. Así ``from app import app; app.openapi()`` funciona en CI sin build
ni hardware.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from engine import Engine
from hub import WsHub
from routes import router as api_router
from settings import API_SCHEMA_VERSION, get_settings

# HTML mínimo cuando la SPA aún no está construida (CI/dev sin `vite build`).
_PLACEHOLDER_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>cam-counter</title></head>
<body style="font-family:system-ui;background:#0c0e12;color:#e6e8ec;padding:2rem">
<h1>cam-counter — API local</h1>
<p>La SPA no está construida (<code>v1/ui/dist</code> no existe).</p>
<p>Construye la UI con <code>cd v1/ui &amp;&amp; npm ci &amp;&amp; npm run build</code>,
o explora la API en <a href="/api/docs" style="color:#ffc400">/api/docs</a>.</p>
</body></html>
"""


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranca/detiene el motor y el hub durante la vida del servidor."""
    settings = get_settings()
    hub = WsHub()
    engine = Engine(settings, hub)
    app.state.hub = hub
    app.state.engine = engine
    await engine.start()
    try:
        yield
    finally:
        await engine.stop()


def create_app() -> FastAPI:
    """Crea y configura la instancia FastAPI (OpenAPI bajo ``/api``)."""
    app = FastAPI(
        title="cam-counter local API",
        # info.version = versión del CONTRATO de la API (estable, reproducible en
        # el snapshot OpenAPI). El app_version derivado de git va en /api/device.
        version=API_SCHEMA_VERSION,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api")
    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    """Sirve la SPA construida en rutas no-``/api`` (fallback a ``index.html``).

    Si ``v1/ui/dist`` no existe, sirve un placeholder en ``/`` y un catch-all; en
    ningún caso rompe el import de la app ni las rutas ``/api``.
    """
    dist = get_settings().ui_dist
    index_html = dist / "index.html"

    if dist.is_dir() and index_html.is_file():
        # Activos estáticos con hash (JS/CSS/img) servidos directamente.
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> Response:
            # Una ruta /api no resuelta es 404 real (no la enmascaramos con la SPA).
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            # Activo estático concreto (favicon, etc.) -> se sirve tal cual.
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(str(candidate))
            # Cualquier otra ruta -> index.html (SPA client-side routing).
            return FileResponse(str(index_html))

    else:

        @app.get("/", include_in_schema=False)
        async def spa_placeholder_root() -> HTMLResponse:
            return HTMLResponse(_PLACEHOLDER_HTML)

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_placeholder(full_path: str) -> Response:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            return HTMLResponse(_PLACEHOLDER_HTML)


app = create_app()
