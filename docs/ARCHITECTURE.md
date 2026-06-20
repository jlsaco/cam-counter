# Arquitectura del producto `cam-counter` (placeholder)

> Placeholder; se amplía en PRs posteriores.

`cam-counter` es un **producto de conteo de personas que cruzan una línea-umbral
configurable**, en tiempo real y en el **borde** (*edge-first*), **multi-cámara y
multi-sitio**. Cada **Raspberry Pi 5 + Hailo-8** captura vídeo de sus cámaras, detecta
personas, hace tracking y cuenta los **cruces de línea** (con histéresis e idempotencia por
track), persistiendo en **local (SQLite WAL)** aunque no haya internet; la nube solo recibe
sincronización/histórico. Una **UI local** (React/Vite/Tailwind servida por FastAPI
*same-origin*) muestra el vídeo en vivo (**MJPEG**) y permite editar la línea de conteo
(**overlay SVG**, coordenadas normalizadas 0..1) con **hot-reload** vía `config_version`.
La flota de Pis se actualiza por **OTA pull-based** (tarball firmado + manifiesto de canal
en S3, instalación atómica con auto-rollback). La infraestructura AWS se gestiona con
**Terraform** (cuenta `950639281773`, región `us-east-1`, prefijo `cam-counter-`).

El sistema **edge histórico** (detector de personas Hailo/YOLO + activador RTSP EZVIZ),
base sobre la que se construye el producto, vive bajo `v1/`. Su bitácora técnica completa
(la odisea del SDK Hikvision bajo box64, el Hailo, el pipeline multi-hilo, …) está en
[`v1/docs/HALLAZGOS.md`](../v1/docs/HALLAZGOS.md).
