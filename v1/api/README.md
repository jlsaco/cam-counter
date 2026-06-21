# `v1/api/` — API + UI local (FastAPI)

**Esqueleto; se implementa en PRs posteriores.**

Backend **FastAPI** que corre en el propio Pi y sirve la SPA local **same-origin** (sin
CORS). Expondrá, entre otros, `/api/device` (con `app_version`), el stream **MJPEG** de
vídeo en vivo y los endpoints de configuración de la línea de conteo (hot-reload vía
`config_version`). Comparte la base de datos **SQLite (WAL)** con el proceso de conteo.

Contratos relevantes en `contracts/`: `line_config.schema.json`, `crossing_event.schema.json`,
`device_registry_item.schema.json`.
