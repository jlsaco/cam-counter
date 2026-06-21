"""Tests de integración de la API (sin Pi/Hailo/cámara, sin red).

Cubre: device, salud de PRODUCTO (frames=0 distinguible de salud real), cámaras,
config GET/PUT (409 por config_version stale + disparo de hot-reload), counters,
reset, histórico paginado, validación de slug y el gate opcional de token.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from cam_counter_edge import Store, compute_event_id
from cam_counter_edge.types import CrossingEvent as EdgeCrossingEvent
from fastapi.testclient import TestClient

import app as app_module
from settings import get_settings


@pytest.fixture
def client(base_env: str) -> Iterator[TestClient]:
    """Cliente en modo SIN hardware (NullSource): lee del SQLite local."""
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture
def fake_client(base_env: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Cliente con la fuente DETERMINISTA activa (CAMCOUNTER_FAKE_SOURCE=1)."""
    monkeypatch.setenv("CAMCOUNTER_FAKE_SOURCE", "1")
    with TestClient(app_module.app) as test_client:
        yield test_client


def _seed_event(db_path: str, *, camera_id: str, seq: int, direction: str, ts_ms: int) -> None:
    """Inserta un CrossingEvent real (bumpea contador) vía el Store del borde."""
    store = Store(db_path)
    try:
        event_id = compute_event_id("demo-site", "demo-pi", camera_id, str(seq), seq)
        store.record_event(
            EdgeCrossingEvent(
                event_id=event_id,
                site_id="demo-site",
                device_id="demo-pi",
                camera_id=camera_id,
                track_id=str(seq),
                crossing_seq=seq,
                direction=direction,
                ts_event_ms=ts_ms,
                ts_event_iso="2023-11-14T22:13:20.000Z",
                clip_status="pending",
            )
        )
    finally:
        store.close()


# -- device ---------------------------------------------------------------- #


def test_device_info(client: TestClient) -> None:
    resp = client.get("/api/device")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "demo-pi"
    assert body["site_id"] == "demo-site"
    assert body["camera_ids"] == ["demo-pi-cam0", "demo-pi-cam1"]
    assert body["db_schema_version"] == 2
    assert isinstance(body["app_version"], str) and body["app_version"]
    assert body["fake_source"] is False


# -- health: producto, no mera liveness ------------------------------------ #


def test_health_frames_zero_is_distinguishable(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"  # schema al día
    assert body["frames_flowing"] is False  # ninguna cámara ha procesado frames
    assert body["db_schema_version"] == 2
    cams = {c["camera_id"]: c for c in body["cameras"]}
    assert cams["demo-pi-cam0"]["frames_processed"] == 0
    assert cams["demo-pi-cam0"]["last_inference_ts"] is None
    assert "config_version" in cams["demo-pi-cam0"]


def test_health_frames_increase_with_fake_source(fake_client: TestClient) -> None:
    # La fuente falsa procesa frames en un hilo de fondo: poll con timeout.
    deadline = time.time() + 5.0
    body: dict = {}
    while time.time() < deadline:
        body = fake_client.get("/api/health").json()
        if body["frames_flowing"]:
            break
        time.sleep(0.05)
    assert body["frames_flowing"] is True
    cam0 = next(c for c in body["cameras"] if c["camera_id"] == "demo-pi-cam0")
    assert cam0["frames_processed"] > 0
    assert cam0["last_inference_ts"] is not None


# -- cámaras --------------------------------------------------------------- #


def test_list_and_get_cameras(client: TestClient) -> None:
    resp = client.get("/api/cameras")
    assert resp.status_code == 200
    ids = [c["camera_id"] for c in resp.json()]
    assert ids == ["demo-pi-cam0", "demo-pi-cam1"]

    one = client.get("/api/cameras/demo-pi-cam0")
    assert one.status_code == 200
    assert one.json()["camera_id"] == "demo-pi-cam0"


@pytest.mark.parametrize(
    "bad_id",
    ["MAYUS", "con%23hash", "x", "a" * 64],
)
def test_invalid_camera_slug_rejected(client: TestClient, bad_id: str) -> None:
    # Slug malformado que SÍ llega al handler -> 400 (validación explícita).
    resp = client.get(f"/api/cameras/{bad_id}")
    assert resp.status_code == 400


def test_slug_with_slash_rejected_by_router(client: TestClient) -> None:
    # '/' (con%2F) parte la ruta en segmentos: el router no la resuelve -> 404.
    # Es igualmente un RECHAZO (un camera_id jamás contiene '/').
    resp = client.get("/api/cameras/con%2Fslash")
    assert resp.status_code in (400, 404)


def test_unknown_camera_404(client: TestClient) -> None:
    resp = client.get("/api/cameras/demo-pi-cam9")
    assert resp.status_code == 404


# -- config: GET / PUT (409 + hot-reload) ---------------------------------- #


def test_get_config_default_v0(client: TestClient) -> None:
    resp = client.get("/api/cameras/demo-pi-cam0/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["config_version"] == 0
    assert body["line"]["a"]["x"] == 0.5
    assert body["positive_side"] in (-1, 1)


def test_put_config_cas_and_hotreload(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = app_module.app.state.engine
    calls: list[tuple[str, int]] = []
    original = engine.notify_config_changed

    async def spy(camera_id: str, config_version: int) -> None:
        calls.append((camera_id, config_version))
        await original(camera_id, config_version)

    monkeypatch.setattr(engine, "notify_config_changed", spy)

    payload = {
        "line": {"a": {"x": 0.4, "y": 0.1}, "b": {"x": 0.4, "y": 0.9}},
        "positive_side": 1,
        "positive_label": "subieron",
        "negative_label": "bajaron",
        "expected_config_version": 0,
    }
    ok = client.put("/api/cameras/demo-pi-cam0/config", json=payload)
    assert ok.status_code == 200
    assert ok.json()["config_version"] == 1
    assert ok.json()["positive_side"] == 1
    # La señal de hot-reload se disparó exactamente una vez con la nueva versión.
    assert calls == [("demo-pi-cam0", 1)]

    # Reintentar con la versión vieja (0) -> 409 (config_version desactualizado).
    stale = client.put("/api/cameras/demo-pi-cam0/config", json=payload)
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["expected"] == 0
    assert detail["current"] == 1

    # Con la versión correcta (1) -> 200 y sube a 2.
    payload2 = {**payload, "expected_config_version": 1}
    ok2 = client.put("/api/cameras/demo-pi-cam0/config", json=payload2)
    assert ok2.status_code == 200
    assert ok2.json()["config_version"] == 2


def test_put_config_invalid_slug_400(client: TestClient) -> None:
    payload = {
        "line": {"a": {"x": 0.4, "y": 0.1}, "b": {"x": 0.4, "y": 0.9}},
        "positive_side": 1,
        "expected_config_version": 0,
    }
    resp = client.put("/api/cameras/MAYUS/config", json=payload)
    assert resp.status_code == 400


# -- counters / reset ------------------------------------------------------ #


def test_counters_and_reset(client: TestClient, base_env: str) -> None:
    _seed_event(base_env, camera_id="demo-pi-cam0", seq=1, direction="in", ts_ms=1_700_000_000_000)
    _seed_event(base_env, camera_id="demo-pi-cam0", seq=2, direction="in", ts_ms=1_700_000_000_500)
    _seed_event(base_env, camera_id="demo-pi-cam0", seq=3, direction="out", ts_ms=1_700_000_001_000)

    body = client.get("/api/cameras/demo-pi-cam0/counters").json()
    assert body["in_count"] == 2
    assert body["out_count"] == 1
    assert body["net"] == 1

    reset = client.post("/api/cameras/demo-pi-cam0/counters/reset")
    assert reset.status_code == 200
    assert reset.json()["in_count"] == 0
    assert reset.json()["out_count"] == 0


# -- histórico paginado ---------------------------------------------------- #


def test_events_pagination(client: TestClient, base_env: str) -> None:
    for i in range(1, 6):
        _seed_event(
            base_env,
            camera_id="demo-pi-cam0",
            seq=i,
            direction="in",
            ts_ms=1_700_000_000_000 + i * 1000,
        )
    page1 = client.get("/api/cameras/demo-pi-cam0/events?limit=2&offset=0").json()
    page2 = client.get("/api/cameras/demo-pi-cam0/events?limit=2&offset=2").json()
    assert len(page1) == 2
    assert len(page2) == 2
    # Orden ts DESC: el más nuevo (seq=5) primero, sin solapes entre páginas.
    assert page1[0]["crossing_seq"] == 5
    ids1 = {e["event_id"] for e in page1}
    ids2 = {e["event_id"] for e in page2}
    assert ids1.isdisjoint(ids2)


# -- gate opcional de token ------------------------------------------------ #


def test_write_token_gate(base_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAMCOUNTER_API_TOKEN", "s3cr3t-lan")  # noqa: S105 (test, no secreto real)
    payload = {
        "line": {"a": {"x": 0.4, "y": 0.1}, "b": {"x": 0.4, "y": 0.9}},
        "positive_side": 1,
        "expected_config_version": 0,
    }
    with TestClient(app_module.app) as client:
        # Sin cabecera -> 401.
        assert client.put("/api/cameras/demo-pi-cam0/config", json=payload).status_code == 401
        # Token incorrecto -> 401.
        bad = client.put(
            "/api/cameras/demo-pi-cam0/config", json=payload, headers={"X-API-Token": "nope"}
        )
        assert bad.status_code == 401
        # Token correcto -> 200.
        good = client.put(
            "/api/cameras/demo-pi-cam0/config",
            json=payload,
            headers={"X-API-Token": "s3cr3t-lan"},
        )
        assert good.status_code == 200
        # Las LECTURAS no requieren token.
        assert client.get("/api/cameras/demo-pi-cam0/config").status_code == 200


# -- SPA same-origin (placeholder cuando no hay dist) ---------------------- #


def test_spa_placeholder_and_api_404(client: TestClient) -> None:
    root = client.get("/")
    assert root.status_code == 200
    assert "cam-counter" in root.text.lower()
    # Una ruta de SPA arbitraria -> también sirve el shell (no /api).
    assert client.get("/cameras/demo-pi-cam0").status_code == 200
    # Una /api inexistente NO se enmascara con la SPA.
    assert client.get("/api/does-not-exist").status_code == 404


def test_dist_path_points_to_ui(client: TestClient) -> None:
    # Documenta el contrato de servido same-origin: dist = v1/ui/dist.
    api_dir = Path(app_module.__file__).resolve().parent
    assert get_settings().ui_dist == api_dir.parent / "ui" / "dist"
