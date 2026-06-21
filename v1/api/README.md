# `v1/api/` — API local (FastAPI) + SPA same-origin

Backend **FastAPI + Uvicorn** que corre en el propio Pi y sirve la SPA local
(`v1/ui/dist`) **same-origin** (sin CORS, sin auth de nube — confianza LAN con un
gate OPCIONAL de token en escrituras). Comparte el **SQLite (WAL)** del borde con
el proceso de conteo; **edge-first**: funciona sin internet.

> Layout **PLANO** a propósito: los módulos (`app.py`, `engine.py`, …) se importan
> como top-level (`from app import app`). Por eso la verificación hace
> `cd v1/api && …` (el cwd entra en `sys.path`).

## Módulos

| Módulo | Rol |
|---|---|
| `schemas.py` | Modelos Pydantic v2 (`CrossingEvent`, `LineConfig`, `Camera`, `DeviceInfo`, `Counters`, `WsEnvelope`, …). `CrossingEvent`/`LineConfig` son ESPEJO EXACTO de `contracts/`. |
| `engine.py` | Adaptador hilo↔asyncio sobre `cam_counter_edge`: lee counters/events/config del SQLite **en un executor de 1 hilo** (no bloquea el event loop), propaga el hot-reload y alimenta el hub WS. |
| `fakes.py` | Fuente DETERMINISTA (MJPEG sintético + cruces guionizados) activada por `CAMCOUNTER_FAKE_SOURCE=1`. Reusa el pipeline real del borde. |
| `mjpeg.py` | Render de frames JPEG sintéticos + envoltura `multipart/x-mixed-replace`. |
| `hub.py` | Hub WebSocket (difusión thread-safe vía `run_coroutine_threadsafe`). |
| `routes.py` | Rutas `/api/*` + WS `/api/ws`. Mapea errores: slug malformado→400, cámara desconocida→404, `config_version` stale→409, token inválido→401. |
| `app.py` | Ensambla FastAPI (OpenAPI en `/api/openapi.json`, docs en `/api/docs`) y monta la SPA con fallback a `index.html` en rutas no-`/api`. |

## Endpoints

- `GET /api/device` — identidad + `app_version` (derivado de `scripts/version.py`).
- `GET /api/health` — **salud de PRODUCTO** por cámara: `frames_processed`
  (monotónico), `last_inference_ts`, `hailo_inference_ok`, `db_schema_version`,
  `config_version`. Un `200` con `frames_processed=0` (`frames_flowing=false`) es
  DISTINGUIBLE de salud real (lo usará el gate de OTA).
- `GET /api/cameras`, `GET /api/cameras/{id}`.
- `GET`/`PUT /api/cameras/{id}/config` — PUT con `expected_config_version`
  desactualizado → **409**; en éxito incrementa `config_version` y dispara la
  señal de hot-reload (WS `config_changed`; la fuente la recoge por su
  `ConfigWatcher`).
- `GET /api/cameras/{id}/counters`, `POST /api/cameras/{id}/counters/reset`.
- `GET /api/cameras/{id}/events?limit=&offset=` — histórico paginado.
- `GET /api/cameras/{id}/stream.mjpg[?frames=N]` — MJPEG (vídeo en vivo).
- `WS /api/ws` — envelopes `counter_update | camera_status | config_changed | crossing`.

## Configuración por entorno (sin secretos)

| Variable | Default | Qué |
|---|---|---|
| `CAMCOUNTER_FAKE_SOURCE` | `0` | `1` activa la fuente determinista (E2E/dev sin Pi). |
| `CAMCOUNTER_DB_PATH` | `v1/api/cam-counter.db` | SQLite del borde (WAL). |
| `CAMCOUNTER_SITE_ID` / `CAMCOUNTER_DEVICE_ID` | `demo-site` / `demo-pi` | Identidad (slugs). |
| `CAMCOUNTER_CAMERA_COUNT` | `2` | Nº de cámaras lógicas. |
| `CAMCOUNTER_API_TOKEN` | _(vacío)_ | Gate OPCIONAL de token en escrituras (`X-API-Token`). Sin él, se permiten (LAN). |
| `CAMCOUNTER_FRAME_INTERVAL` | `0.2` | Cadencia MJPEG / fuente falsa (s). |

## Correr en local (sin Pi/Hailo/cámara)

```bash
python -m pip install -e v1/edge                      # provee cam_counter_edge
python -m pip install -r v1/api/requirements.txt
cd v1/ui && npm ci && npm run build && cd -            # genera v1/ui/dist (opcional)
cd v1/api && CAMCOUNTER_FAKE_SOURCE=1 \
  python -m uvicorn app:app --host 0.0.0.0 --port 8000
# UI:   http://localhost:8000/
# Docs: http://localhost:8000/api/docs
```

Si `v1/ui/dist` no existe, la app degrada con elegancia (sirve un placeholder y la
API sigue operativa).

## Verificación

```bash
cd v1/api && ruff check . && mypy . && python3 -m pytest -q
cd v1/api && CAMCOUNTER_FAKE_SOURCE=1 \
  python3 -c 'from app import app; print(len(app.openapi()["paths"]))'
```

## Snapshot OpenAPI

`openapi.snapshot.json` está commiteado y `tests/test_openapi_snapshot.py` lo
compara con `app.openapi()` (drift ⇒ build rojo). Tras un cambio INTENCIONADO de
la API, regenéralo:

```bash
cd v1/api && python -m gen_openapi_snapshot
```

`info.version` del OpenAPI es la **versión del CONTRATO** de la API (estable, para
que el snapshot sea reproducible); el `app_version` derivado de git va en
`/api/device`.

## systemd (independiente de `hailo-personas`)

La unit `v1/systemd/cam-counter-api.service` NO tiene rutas absolutas fijas: usa el
placeholder `__CAM_COUNTER_REPO__`. Renderiza/instala con:

```bash
scripts/install_api_service.sh                 # idempotente; en x86 sólo renderiza
NO_SYSTEMCTL=1 UNIT_DEST=/tmp/x.service scripts/install_api_service.sh   # dry-run
```
