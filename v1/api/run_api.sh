#!/usr/bin/env bash
# Lanzador de la API local (Uvicorn) para la unit systemd cam-counter-api.
#
# Resuelve la ruta del repo de forma robusta (sin rutas absolutas fijas), elige
# el intérprete (venv del repo si existe, si no python3 del sistema) y arranca
# Uvicorn sirviendo `app:app` (la app FastAPI + SPA same-origin).
#
# Host/puerto configurables por entorno: CAMCOUNTER_HOST (def 0.0.0.0),
# CAMCOUNTER_PORT (def 8000). Pasa `bash -n`.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../<repo>/v1/api
repo="$(cd "$here/../.." && pwd)"

if [ -x "$repo/.venv/bin/python" ]; then
  PY="$repo/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

cd "$here"   # WorkingDirectory = v1/api: `app:app` (layout plano) importable
exec "$PY" -m uvicorn app:app \
  --host "${CAMCOUNTER_HOST:-0.0.0.0}" \
  --port "${CAMCOUNTER_PORT:-8000}"
