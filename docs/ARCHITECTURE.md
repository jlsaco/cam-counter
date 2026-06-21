# Arquitectura — `cam-counter`

> Placeholder de docs de producto; se amplía en PRs posteriores.

`cam-counter` evoluciona de un detector de personas en el borde (Raspberry Pi 5 + Hailo-8 +
YOLOv8s, cámara EZVIZ por RTSP) hacia un **producto de conteo de personas que cruzan una
línea-umbral configurable**, en tiempo real, **edge-first**, **multi-cámara y multi-sitio**.
Cada Pi captura, detecta (Hailo), trackea, cuenta el **cruce de línea** (con histéresis e
idempotencia por track) y **persiste en local (SQLite WAL)** aunque no haya internet; una
**API + UI local** servida desde el propio Pi (FastAPI + SPA React/Vite/Tailwind,
same-origin) muestra el vídeo en vivo (**MJPEG**) y permite editar la línea como overlay SVG
en coordenadas normalizadas 0..1 con hot-reload. La nube AWS (Terraform) sólo recibe
sincronización/histórico (media en S3, eventos y device-registry en DynamoDB), y una flota
de Pis se actualiza por **OTA pull-based** con artefactos firmados y manifiesto de versión
deseada por canal.

La **historia del edge** (cómo se hizo funcionar el Hailo y el activador RTSP de la cámara,
los 10 obstáculos resueltos) está documentada en
[`v1/docs/HALLAZGOS.md`](../v1/docs/HALLAZGOS.md). El sistema edge histórico vive bajo
[`v1/`](../v1/); los subsistemas nuevos (API/UI, infra, OTA) se construyen sobre esa base
como una pila de PRs apilados (ver [`CONTRIBUTING.md`](CONTRIBUTING.md)).
