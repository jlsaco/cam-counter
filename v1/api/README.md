# `v1/api/` — API + UI local (FastAPI)

**Esqueleto; se implementa en PRs posteriores.**

Backend **FastAPI** que corre en el propio Pi y sirve la SPA local **same-origin** (sin
CORS). Expondrá, entre otros, `/api/device` (con `app_version`), el stream **MJPEG** de
vídeo en vivo y los endpoints de configuración de la línea de conteo (hot-reload vía
`config_version`). Comparte la base de datos **SQLite (WAL)** con el proceso de conteo.

Contratos relevantes en `contracts/`: `line_config.schema.json`, `crossing_event.schema.json`,
`device_registry_item.schema.json`.

## PR10 — harness mínimo (provisional)

Mientras PR09 (FastAPI completo + SPA React) no esté en esta base, `app.py` es un
**harness MÍNIMO** self-contained que implementa lo imprescindible para el E2E de
PR10: `GET`/`PUT /api/line` (config de línea con `config_version` monótono,
persistida en local), `GET /api/counters`, `WS /api/ws` (incremento en vivo),
`GET /api/stream` (MJPEG fake) y sirve la SPA estática de `../ui/public`. La fuente
fake se activa con `CAMCOUNTER_FAKE_SOURCE=1`. Tests sin navegador en `tests/`:

```bash
python -m pip install fastapi "uvicorn[standard]" httpx websockets pytest
cd v1/api && python -m pytest -q
```
