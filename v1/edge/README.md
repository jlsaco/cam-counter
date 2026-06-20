# `v1/edge/` — paquete de conteo edge (Python)

**Esqueleto; se implementa en PRs posteriores.**

Paquete Python del **contador de personas en el borde**. Pipeline:
`captura → detect (Hailo) → track → count → present + clip + sink`.

- `count` = detección de **cruce de línea** con **histéresis** (banda muerta anti-rebote) e
  **idempotencia por track** (un mismo `track_id` no recuenta el mismo cruce); emite eventos
  conformes a `contracts/crossing_event.schema.json`.
- Un **`DummyDetector`** permitirá ejercitar toda la lógica de conteo en x86 sin hardware
  Hailo ni cámara, de modo que la verificación corra en CI x86.
- `present` emite **MJPEG**; `clip` graba el recorte del evento; `sink` persiste en
  **SQLite (WAL)**.

El detector histórico actual vive en `v1/detection/` y el activador RTSP en
`v1/rtsp-enable/`; este paquete construye el producto de conteo sobre esa base.
