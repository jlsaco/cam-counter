# syntax=docker/dockerfile:1
#
# docker/api.Dockerfile — imagen del servicio `api` (WP17): FastAPI + UI same-origin.
#
# A diferencia de `edge`, esta imagen NO necesita Hailo, NO necesita certificados y
# NO abre /dev/hailo0: sólo sirve la API REST/WS local y la SPA construida, leyendo
# el MISMO SQLite del borde (montado read-only). Por eso es una imagen multi-arch
# limpia (python slim), construible en cualquier arquitectura (incluido CI x86).
#
# Multi-stage:
#   1) ui-build : Node construye la SPA (vite build -> v1/ui/dist).
#   2) runtime  : python slim con FastAPI/Uvicorn + el paquete cam_counter_edge
#                 (sólo para el Store/identificadores; sus deps de hardware se
#                 importan perezosas y NUNCA se cargan aquí) + la SPA construida.
#
# El layout del repo se REPLICA bajo /opt/cam-counter para que settings.ui_dist
# (= v1/api/../ui/dist) resuelva sin tocar el código.
#
# Build (contexto = raíz del repo):
#     docker build -f docker/api.Dockerfile -t cam-counter-api:dev .

# --- stage 1: build de la SPA ------------------------------------------------
FROM node:22-slim AS ui-build
WORKDIR /ui
# npm ci reproducible (usa package-lock.json); sólo se invalida si cambian deps.
COPY v1/ui/package.json v1/ui/package-lock.json ./
RUN npm ci
COPY v1/ui/ ./
RUN npm run build   # tsc --noEmit && vite build -> /ui/dist

# --- stage 2: runtime FastAPI ------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # cam_counter_edge importable (Store + identificadores). Sus deps de hardware
    # (cv2/hailo) se importan PEREZOSAMENTE y no se instalan en esta imagen.
    PYTHONPATH=/opt/cam-counter/v1/edge \
    CAMCOUNTER_HOST=0.0.0.0 \
    CAMCOUNTER_PORT=8088 \
    CAMCOUNTER_DB_PATH=/var/lib/cam-counter/cam-counter.db

WORKDIR /opt/cam-counter/v1/api

# Dependencias de la API (versiones FIJADAS en requirements.txt para reproducir el
# snapshot OpenAPI) + numpy (dep base de cam_counter_edge). curl para el HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY v1/api/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir -r /tmp/api-requirements.txt "numpy>=1.24"

# Código: paquete de borde (sólo Python) + app de la API. La SPA construida se
# coloca donde settings.ui_dist la espera (v1/ui/dist).
COPY v1/edge/ /opt/cam-counter/v1/edge/
COPY v1/api/ /opt/cam-counter/v1/api/
COPY --from=ui-build /ui/dist /opt/cam-counter/v1/ui/dist
RUN python -c "import app; print('FastAPI app OK')"

# HEALTHCHECK: liveness plano a /healthz (no toca la DB; sirve aun con db :ro).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${CAMCOUNTER_PORT}/healthz" || exit 1

# Uvicorn sirve app:app (API /api/* + SPA same-origin). Host/puerto por entorno.
CMD ["sh", "-c", "exec python -m uvicorn app:app --host \"${CAMCOUNTER_HOST}\" --port \"${CAMCOUNTER_PORT}\""]
