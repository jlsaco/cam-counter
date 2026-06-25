"""Tests del handler `cam-counter-fleet-api` (read-only, paginado).

Ejercita las tres rutas, la fusión de canales del GSI1, la paginación por cursor opaco (sin
duplicar ni perder items al cruzar la frontera de canal), el orden descendente de eventos, el
saneado de Decimals + el stripping de claves internas, y los errores 400/404. Usa `FakeTable`
en memoria: SIN AWS real.
"""

from __future__ import annotations

import importlib
import json
from decimal import Decimal

import pytest
from fakeddb import FakeTable


def _device(device_id, site_id, channel, cameras):
    return {
        "PK": f"DEVICE#{device_id}",
        "GSI1PK": f"CHANNEL#{channel}",
        "GSI1SK": f"DEVICE#{device_id}",
        "device_id": device_id,
        "site_id": site_id,
        "release_channel": channel,
        "camera_ids": cameras,
        "reported_version": "1.2.3",
        "status": "online",
    }


def _event(site_id, device_id, camera_id, ts_ms, event_id, seq):
    return {
        "PK": f"CAM#{site_id}#{device_id}#{camera_id}",
        "SK": f"TS#{ts_ms:013d}#{event_id}",
        "GSI1PK": f"SITE#{site_id}",
        "GSI1SK": f"TS#{ts_ms:013d}#{event_id}",
        "event_id": event_id,
        "site_id": site_id,
        "device_id": device_id,
        "camera_id": camera_id,
        "ts_event_ms": Decimal(ts_ms),
        "crossing_seq": Decimal(seq),
        "direction": "in",
        "confidence": Decimal("0.91"),
    }


DEVICES = [
    _device("rpi-001", "sitio-demo", "stable", ["rpi-001-cam0"]),
    _device("rpi-002", "sitio-demo", "stable", ["rpi-002-cam0", "rpi-002-cam1"]),
    _device("rpi-003", "sitio-demo", "stable", ["rpi-003-cam0"]),
    _device("rpi-010", "sitio-demo", "canary", ["rpi-010-cam0"]),
    _device("rpi-011", "sitio-demo", "canary", ["rpi-011-cam0"]),
]

EVENTS = [
    _event("sitio-demo", "rpi-001", "rpi-001-cam0", 1_700_000_000_000 + i, "e" + str(i) * 39, i)
    for i in range(5)
]


@pytest.fixture()
def handler(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("EVENTS_TABLE", "cam-counter-events")
    monkeypatch.setenv("DEVICES_TABLE", "cam-counter-devices")
    import ddb

    importlib.reload(ddb)
    import handler as mod

    importlib.reload(mod)
    monkeypatch.setattr(ddb, "_devices_table", lambda: FakeTable(DEVICES))
    monkeypatch.setattr(ddb, "_events_table", lambda: FakeTable(EVENTS))
    return mod


def _invoke(mod, route_key, path=None, qs=None):
    resp = mod.lambda_handler(
        {"routeKey": route_key, "pathParameters": path, "queryStringParameters": qs}
    )
    return resp["statusCode"], json.loads(resp["body"])


# ───────────────────────── /devices ─────────────────────────


def test_list_all_devices_merges_channels(handler):
    status, body = _invoke(handler, "GET /devices")
    assert status == 200
    ids = {d["device_id"] for d in body["devices"]}
    assert ids == {"rpi-001", "rpi-002", "rpi-003", "rpi-010", "rpi-011"}
    assert body["next_cursor"] is None
    # Las claves internas no se filtran al cliente.
    assert all("PK" not in d and "GSI1PK" not in d for d in body["devices"])


def test_list_devices_by_channel(handler):
    status, body = _invoke(handler, "GET /devices", qs={"channel": "canary"})
    assert status == 200
    assert {d["device_id"] for d in body["devices"]} == {"rpi-010", "rpi-011"}


def test_list_devices_bad_channel(handler):
    status, body = _invoke(handler, "GET /devices", qs={"channel": "beta"})
    assert status == 400
    assert "error" in body


def test_list_devices_pagination_across_channels(handler):
    """Pagina de 2 en 2 cruzando stable->canary sin duplicar ni perder dispositivos."""
    seen = []
    cursor = None
    for _ in range(10):  # cota dura anti-bucle
        qs = {"limit": "2"}
        if cursor:
            qs["cursor"] = cursor
        status, body = _invoke(handler, "GET /devices", qs=qs)
        assert status == 200
        assert len(body["devices"]) <= 2
        seen.extend(d["device_id"] for d in body["devices"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == ["rpi-001", "rpi-002", "rpi-003", "rpi-010", "rpi-011"]
    assert len(seen) == len(set(seen))  # sin duplicados


def test_list_devices_bad_cursor(handler):
    status, _ = _invoke(handler, "GET /devices", qs={"cursor": "!!!not-base64!!!"})
    assert status == 400


def test_list_devices_bad_limit(handler):
    status, _ = _invoke(handler, "GET /devices", qs={"limit": "cero"})
    assert status == 400


# ───────────────────────── /devices/{id} ─────────────────────────


def test_get_device_ok(handler):
    status, body = _invoke(handler, "GET /devices/{deviceId}", path={"deviceId": "rpi-002"})
    assert status == 200
    assert body["device"]["device_id"] == "rpi-002"
    assert body["device"]["camera_ids"] == ["rpi-002-cam0", "rpi-002-cam1"]
    assert "PK" not in body["device"]


def test_get_device_not_found(handler):
    status, body = _invoke(handler, "GET /devices/{deviceId}", path={"deviceId": "rpi-999"})
    assert status == 404


def test_get_device_bad_slug(handler):
    status, _ = _invoke(handler, "GET /devices/{deviceId}", path={"deviceId": "BAD/slug"})
    assert status == 400


# ───────────────────────── /devices/{id}/events ─────────────────────────


def test_list_events_default_camera_desc_order(handler):
    status, body = _invoke(
        handler, "GET /devices/{deviceId}/events", path={"deviceId": "rpi-001"}
    )
    assert status == 200
    assert body["camera_id"] == "rpi-001-cam0"
    ts = [e["ts_event_ms"] for e in body["events"]]
    assert ts == sorted(ts, reverse=True)  # más recientes primero
    # Decimal saneado a int nativo.
    assert all(isinstance(e["crossing_seq"], int) for e in body["events"])


def test_list_events_unknown_camera(handler):
    status, _ = _invoke(
        handler,
        "GET /devices/{deviceId}/events",
        path={"deviceId": "rpi-001"},
        qs={"camera": "rpi-001-cam9"},
    )
    assert status == 404


def test_list_events_pagination(handler):
    seen = []
    cursor = None
    for _ in range(10):
        qs = {"limit": "2"}
        if cursor:
            qs["cursor"] = cursor
        status, body = _invoke(
            handler, "GET /devices/{deviceId}/events", path={"deviceId": "rpi-001"}, qs=qs
        )
        assert status == 200
        seen.extend(e["event_id"] for e in body["events"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 5
    assert len(seen) == len(set(seen))


def test_list_events_device_not_found(handler):
    status, _ = _invoke(
        handler, "GET /devices/{deviceId}/events", path={"deviceId": "rpi-999"}
    )
    assert status == 404


def test_unknown_route(handler):
    status, _ = _invoke(handler, "POST /devices")
    assert status == 404
