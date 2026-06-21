"""Snapshot de ``/api/openapi.json``: detecta drift de paths/campos en CI.

El snapshot commiteado (``v1/api/openapi.snapshot.json``) debe coincidir con
``app.openapi()``. Si cambia un path o un campo de un modelo sin regenerar el
snapshot, este test (y por tanto el build) se pone ROJO.

Regenerar tras un cambio INTENCIONADO de la API:
    cd v1/api && python -m gen_openapi_snapshot
"""

from __future__ import annotations

import json
from pathlib import Path

from app import app

_SNAPSHOT = Path(__file__).resolve().parents[1] / "openapi.snapshot.json"


def test_openapi_snapshot_matches() -> None:
    current = app.openapi()
    committed = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert current == committed, (
        "El OpenAPI vivo difiere del snapshot commiteado. Si el cambio es "
        "intencionado, regenera con `cd v1/api && python -m gen_openapi_snapshot`."
    )


def test_openapi_served_under_api_prefix() -> None:
    assert app.openapi_url == "/api/openapi.json"


def test_openapi_info_version_is_stable() -> None:
    # info.version = versión del CONTRATO de la API (estable), NO el app_version.
    assert app.openapi()["info"]["version"] == "1.0.0"
