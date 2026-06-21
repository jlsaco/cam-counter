#!/usr/bin/env bash
# Lanzador del supervisor de borde (cam-counter-edge) para la unit systemd
# cam-counter-edge.service.
#
# Resuelve la ruta del repo de forma robusta (sin rutas absolutas fijas), elige el
# interprete (venv del repo si existe, si no python3 del sistema) y arranca el
# entrypoint `cam-counter-edge` (modulo cam_counter_edge.app:main). Pasa `bash -n`.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../<repo>/v1/edge
repo="$(cd "$here/../.." && pwd)"

if [ -x "$repo/.venv/bin/python" ]; then
  PY="$repo/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

cd "$here"   # WorkingDirectory = v1/edge: el paquete cam_counter_edge es importable
exec "$PY" -m cam_counter_edge.app
