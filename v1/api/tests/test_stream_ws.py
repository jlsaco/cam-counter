"""Tests del stream MJPEG y del hub WebSocket (sin hardware)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app as app_module
import mjpeg


@pytest.fixture
def client(base_env: str) -> Iterator[TestClient]:
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_mjpeg_stream_bounded(client: TestClient) -> None:
    # ?frames=2 acota el stream a 2 frames (modo sin hardware: 'sin señal').
    resp = client.get("/api/cameras/demo-pi-cam0/stream.mjpg?frames=2")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("multipart/x-mixed-replace")
    body = resp.content
    boundary = f"--{mjpeg.MULTIPART_BOUNDARY}".encode()
    assert body.count(boundary) == 2
    assert b"Content-Type: image/jpeg" in body
    # JPEG SOI marker presente (frame real, no vacío).
    assert b"\xff\xd8\xff" in body


def test_mjpeg_stream_invalid_slug_400(client: TestClient) -> None:
    assert client.get("/api/cameras/MAYUS/stream.mjpg?frames=1").status_code == 400


def test_ws_receives_config_changed(client: TestClient) -> None:
    payload = {
        "line": {"a": {"x": 0.4, "y": 0.1}, "b": {"x": 0.4, "y": 0.9}},
        "positive_side": 1,
        "expected_config_version": 0,
    }
    with client.websocket_connect("/api/ws") as ws:
        resp = client.put("/api/cameras/demo-pi-cam0/config", json=payload)
        assert resp.status_code == 200
        msg = ws.receive_json()
        assert msg["type"] == "config_changed"
        assert msg["camera_id"] == "demo-pi-cam0"
        assert msg["data"]["config_version"] == 1
