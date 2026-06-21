"""Tests del harness FastAPI (``v1/api/app.py``) SIN navegador.

Verifican en x86 (corre en cualquier sitio, también en el Pi/ARM donde Playwright
no tiene chromium) el contrato que la suite Playwright E2E ejercita por la UI:

- ``GET``/``PUT /api/line`` con ``config_version`` MONÓTONO y persistencia LOCAL
  (sobrevive a un "reload"/reinicio del proceso),
- la fuente fake (``CAMCOUNTER_FAKE_SOURCE=1``) incrementa los contadores y los
  empuja por WebSocket (incremento en vivo),
- la SPA estática se sirve same-origin en ``/``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Hace importable ``app`` desde ``v1/api/`` sin instalar el paquete.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fresh_app(monkeypatch: pytest.MonkeyPatch, state_file: Path, **env: str) -> Any:
    monkeypatch.setenv("CAMCOUNTER_LINE_STATE", str(state_file))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module("app")
    importlib.reload(mod)
    return mod.create_app()


def test_get_default_line_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _fresh_app(monkeypatch, tmp_path / "line.json")
    with TestClient(app) as client:
        cfg = client.get("/api/line").json()
    assert cfg["camera_id"] == "rpi-001-cam0"
    assert cfg["positive_side"] in (-1, 1)
    assert 0.0 <= cfg["line"]["a"]["x"] <= 1.0  # coords normalizadas 0..1


def test_put_line_bumps_config_version_and_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "line.json"
    app = _fresh_app(monkeypatch, state_file)
    new_line = {
        "site_id": "sitio-demo", "device_id": "rpi-001", "camera_id": "rpi-001-cam0",
        "config_version": 1,
        "line": {"a": {"x": 0.2, "y": 0.3}, "b": {"x": 0.8, "y": 0.7}},
        "positive_side": -1, "positive_label": "bajaron", "negative_label": "subieron",
        "schema_version": 1,
    }
    with TestClient(app) as client:
        before = client.get("/api/line").json()["config_version"]
        saved = client.put("/api/line", json=new_line).json()
        assert saved["config_version"] > before  # MONÓTONO (hot-reload)
        assert saved["positive_side"] == -1
        assert saved["line"]["a"]["x"] == 0.2

    # "Reload": una instancia NUEVA del proceso relee la config persistida en local.
    app2 = _fresh_app(monkeypatch, state_file)
    with TestClient(app2) as client2:
        reloaded = client2.get("/api/line").json()
    assert reloaded["line"]["b"]["y"] == 0.7
    assert reloaded["positive_side"] == -1
    assert reloaded["config_version"] == saved["config_version"]


def test_fake_source_increments_counters_over_ws(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _fresh_app(
        monkeypatch,
        tmp_path / "line.json",
        CAMCOUNTER_FAKE_SOURCE="1",
        CAMCOUNTER_FAKE_INTERVAL="0.05",
    )
    with TestClient(app) as client, client.websocket_connect("/api/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "counters"
        initial = first["counters"]["in"] + first["counters"]["out"]
        # La fuente fake empuja incrementos en vivo por WS.
        latest = initial
        for _ in range(10):
            msg = ws.receive_json()
            latest = msg["counters"]["in"] + msg["counters"]["out"]
            if latest > initial:
                break
        assert latest > initial


def test_spa_served_same_origin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _fresh_app(monkeypatch, tmp_path / "line.json")
    with TestClient(app) as client:
        res = client.get("/")
    assert res.status_code == 200
    assert "overlay" in res.text and 'data-testid="btn-save"' in res.text
