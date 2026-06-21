"""Configuración y fixtures de pytest para la API (layout PLANO de módulos).

Inserta el directorio ``v1/api`` en ``sys.path`` para que ``app``, ``engine``,
``schemas``, ... se importen como módulos top-level tanto al correr ``pytest``
desde ``v1/api`` como desde la raíz del repo. Define fixtures de entorno y de
``TestClient`` que NO tocan hardware (fuente falsa o modo sin hardware) ni red.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_API_DIR = str(Path(__file__).resolve().parent)
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Entorno base reproducible: DB temporal, identidad fija, sin token/fake."""
    db_path = str(tmp_path / "cam-counter.db")
    monkeypatch.setenv("CAMCOUNTER_DB_PATH", db_path)
    monkeypatch.setenv("CAMCOUNTER_SITE_ID", "demo-site")
    monkeypatch.setenv("CAMCOUNTER_DEVICE_ID", "demo-pi")
    monkeypatch.setenv("CAMCOUNTER_CAMERA_COUNT", "2")
    monkeypatch.setenv("CAMCOUNTER_FRAME_INTERVAL", "0.02")
    monkeypatch.delenv("CAMCOUNTER_API_TOKEN", raising=False)
    monkeypatch.delenv("CAMCOUNTER_FAKE_SOURCE", raising=False)
    return db_path
