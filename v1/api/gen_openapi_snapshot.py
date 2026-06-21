"""Regenera el snapshot commiteado de ``/api/openapi.json``.

Uso (desde ``v1/api``):
    python -m gen_openapi_snapshot

Escribe ``openapi.snapshot.json`` con claves ordenadas e indentación estable. El
test ``tests/test_openapi_snapshot.py`` compara ``app.openapi()`` con este fichero
y se pone rojo ante cualquier drift no regenerado.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import app

SNAPSHOT_PATH = Path(__file__).resolve().parent / "openapi.snapshot.json"


def write_snapshot() -> Path:
    """Serializa ``app.openapi()`` al fichero de snapshot y devuelve su ruta."""
    spec = app.openapi()
    SNAPSHOT_PATH.write_text(
        json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return SNAPSHOT_PATH


if __name__ == "__main__":
    path = write_snapshot()
    print(f"OpenAPI snapshot escrito en {path}")
