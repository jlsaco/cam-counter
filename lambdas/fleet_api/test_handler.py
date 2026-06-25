"""Tests del handler `cam-counter-fleet-api` (read-only) con DynamoDB simulado por moto.

Ejercita las tres rutas, el cursor opaco base64, la enumeración por canal vía Query del GSI1
(sin Scan), la validación de slugs y los códigos 400/404. Corre en x86 sin AWS real.
"""

from __future__ import annotations

import base64
import importlib
import json
import os

import boto3
import pytest
from moto import mock_aws

EVENTS_TABLE = "cam-counter-events"
DEVICES_TABLE = "cam-counter-devices"
GSI1 = "GSI1"


@pytest.fixture()
def handler(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("EVENTS_TABLE", EVENTS_TABLE)
    monkeypatch.setenv("DEVICES_TABLE", DEVICES_TABLE)
    monkeypatch.setenv("DEVICES_GSI1", GSI1)
    monkeypatch.setenv("KNOWN_CHANNELS", "canary,stable")
    monkeypatch.setenv("DEFAULT_PAGE_SIZE", "50")
    monkeypatch.setenv("MAX_PAGE_SIZE", "100")
    with mock_aws():
        _seed()
        # Import perezoso DENTRO del mock para que el resource boto3 module-level use moto.
        import handler as mod

        importlib.reload(mod)
        yield mod


def _seed() -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=DEVICES_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": GSI1,
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    ddb.create_table(
        TableName=EVENTS_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": GSI1,
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )

    devices = boto3.resource("dynamodb", region_name="us-east-1").Table(DEVICES_TABLE)
    devices.put_item(
        Item={
            "PK": "DEVICE#rpi-001",
            "GSI1PK": "CHANNEL#stable",
            "GSI1SK": "DEVICE#rpi-001",
            "device_id": "rpi-001",
            "site_id": "sitio-demo",
            "camera_ids": ["rpi-001-cam0"],
            "release_channel": "stable",
            "schema_version": 1,
        }
    )
    devices.put_item(
        Item={
            "PK": "DEVICE#rpi-002",
            "GSI1PK": "CHANNEL#canary",
            "GSI1SK": "DEVICE#rpi-002",
            "device_id": "rpi-002",
            "site_id": "sitio-demo",
            "camera_ids": ["rpi-002-cam0", "rpi-002-cam1"],
            "release_channel": "canary",
            "schema_version": 1,
        }
    )

    events = boto3.resource("dynamodb", region_name="us-east-1").Table(EVENTS_TABLE)
    for i in range(5):
        ts = 1_700_000_000_000 + i
        eid = f"{i:040x}"
        events.put_item(
            Item={
                "PK": "CAM#sitio-demo#rpi-001#rpi-001-cam0",
                "SK": f"TS#{ts:013d}#{eid}",
                "GSI1PK": "SITE#sitio-demo",
                "GSI1SK": f"TS#{ts:013d}#{eid}",
                "event_id": eid,
                "site_id": "sitio-demo",
                "device_id": "rpi-001",
                "camera_id": "rpi-001-cam0",
                "track_id": str(i),
                "crossing_seq": i,
                "direction": "in",
                "ts_event_ms": ts,
                "ts_event_iso": "2023-11-14T22:13:20Z",
                "schema_version": 1,
            }
        )


def _invoke(mod, route_key, path=None, qs=None):
    resp = mod.lambda_handler({"routeKey": route_key, "pathParameters": path, "queryStringParameters": qs})
    return resp["statusCode"], json.loads(resp["body"])


# ───────────────────────── GET /devices ─────────────────────────
def test_list_devices_merges_channels(handler):
    status, body = _invoke(handler, "GET /devices")
    assert status == 200
    ids = sorted(d["device_id"] for d in body["devices"])
    assert ids == ["rpi-001", "rpi-002"]
    assert body["count"] == 2
    # No se filtran claves internas al cliente.
    assert all("PK" not in d and "GSI1PK" not in d for d in body["devices"])


def test_list_devices_filter_channel(handler):
    status, body = _invoke(handler, "GET /devices", qs={"channel": "canary"})
    assert status == 200
    assert [d["device_id"] for d in body["devices"]] == ["rpi-002"]


def test_list_devices_bad_channel(handler):
    status, body = _invoke(handler, "GET /devices", qs={"channel": "beta"})
    assert status == 400
    assert "channel" in body["error"]


# ───────────────────────── GET /devices/{id} ─────────────────────────
def test_get_device_ok(handler):
    status, body = _invoke(handler, "GET /devices/{deviceId}", path={"deviceId": "rpi-001"})
    assert status == 200
    assert body["device"]["device_id"] == "rpi-001"
    assert body["device"]["site_id"] == "sitio-demo"


def test_get_device_404(handler):
    status, body = _invoke(handler, "GET /devices/{deviceId}", path={"deviceId": "rpi-999"})
    assert status == 404


def test_get_device_bad_slug(handler):
    status, _ = _invoke(handler, "GET /devices/{deviceId}", path={"deviceId": "Bad/Id"})
    assert status == 400


# ───────────────────────── GET /devices/{id}/events ─────────────────────────
def test_list_events_newest_first(handler):
    status, body = _invoke(handler, "GET /devices/{deviceId}/events", path={"deviceId": "rpi-001"})
    assert status == 200
    seqs = [e["crossing_seq"] for e in body["events"]]
    assert seqs == sorted(seqs, reverse=True)  # ScanIndexForward=false
    assert body["count"] == 5
    assert body["next_cursor"] is None


def test_list_events_cursor_pagination(handler):
    status, page1 = _invoke(
        handler, "GET /devices/{deviceId}/events", path={"deviceId": "rpi-001"}, qs={"limit": "2"}
    )
    assert status == 200
    assert page1["count"] == 2
    assert page1["next_cursor"] is not None
    # Cursor OPACO: base64 decodificable a un dict (el cliente no lo interpreta).
    decoded = json.loads(base64.urlsafe_b64decode(page1["next_cursor"]).decode())
    assert isinstance(decoded, dict)

    status, page2 = _invoke(
        handler,
        "GET /devices/{deviceId}/events",
        path={"deviceId": "rpi-001"},
        qs={"limit": "2", "cursor": page1["next_cursor"]},
    )
    assert status == 200
    first_seqs = {e["crossing_seq"] for e in page1["events"]}
    second_seqs = {e["crossing_seq"] for e in page2["events"]}
    assert first_seqs.isdisjoint(second_seqs)  # sin solape entre páginas


def test_list_events_bad_cursor(handler):
    status, body = _invoke(
        handler, "GET /devices/{deviceId}/events", path={"deviceId": "rpi-001"}, qs={"cursor": "!!!notb64"}
    )
    assert status == 400
    assert "cursor" in body["error"]


def test_list_events_multi_camera_requires_param(handler):
    status, body = _invoke(handler, "GET /devices/{deviceId}/events", path={"deviceId": "rpi-002"})
    assert status == 400
    assert "camera_id" in body["error"]


def test_list_events_explicit_camera(handler):
    status, body = _invoke(
        handler,
        "GET /devices/{deviceId}/events",
        path={"deviceId": "rpi-001"},
        qs={"camera_id": "rpi-001-cam0"},
    )
    assert status == 200
    assert body["count"] == 5


def test_unknown_route_404(handler):
    status, _ = _invoke(handler, "DELETE /devices/{deviceId}", path={"deviceId": "rpi-001"})
    assert status == 404
