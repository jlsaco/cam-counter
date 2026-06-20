# v1/api — API + UI local (esqueleto)

Esqueleto; **se implementa en PRs posteriores**.

Backend **FastAPI** que corre en el propio Pi y sirve la SPA local (`v1/ui`) *same-origin*
(sin CORS). Expone el stream **MJPEG** en vivo, la configuración de la línea de conteo
(overlay SVG en coordenadas normalizadas 0..1), los eventos de cruce y `/api/device`.

Comparte la **SQLite (modo WAL)** con el proceso de conteo (`v1/edge`). El **hot-reload de
configuración** se hace vía `config_version` (cambiar la línea no reinicia el servicio).

> Aquí solo queda el esqueleto: los modelos Pydantic, endpoints y lógica llegan en PRs
> posteriores. Los contratos canónicos viven en `contracts/`.
