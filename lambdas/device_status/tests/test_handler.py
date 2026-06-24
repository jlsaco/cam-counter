"""Tests de la Lambda device-status: guard de no-retroceso, GSI1 por canal, status."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

import handler


class FakeDevices:
    """Tabla de dispositivos falsa con el guard ``last_seen_ms <= :now``."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.update_calls = 0

    def update_item(
        self,
        Key,  # noqa: N803
        UpdateExpression,  # noqa: N803
        ExpressionAttributeNames,  # noqa: N803
        ExpressionAttributeValues,  # noqa: N803
        ConditionExpression,  # noqa: N803
        **_kw,
    ):
        self.update_calls += 1
        pk = Key["PK"]
        current = self.items.get(pk, {})
        now = ExpressionAttributeValues[":now"]
        last = current.get("last_seen_ms")
        # Reproduce: attribute_not_exists(last_seen_ms) OR last_seen_ms <= :now
        if last is not None and not (last <= now):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "stale"}},
                "UpdateItem",
            )
        # Aplica el SET: mapea cada "#name = :val" usando los diccionarios de alias.
        item = dict(current)
        for assign in UpdateExpression[len("SET ") :].split(", "):
            name_alias, val_alias = (s.strip() for s in assign.split("="))
            item[ExpressionAttributeNames[name_alias]] = ExpressionAttributeValues[val_alias]
        self.items[pk] = item


BASE_TS = 1_718_900_000_000


def make_event(**overrides):
    ev = {
        "device_id": "rpi-001",
        "status": "online",
        "release_channel": "stable",
        "reported_version": "1.2.3",
        "_ingest_ts_ms": BASE_TS,
    }
    ev.update(overrides)
    return ev


def test_upsert_basico_escribe_last_seen_y_status():
    table = FakeDevices()
    out = handler.process(make_event(), table)
    assert out == {"ok": True, "stale": False}
    item = table.items["DEVICE#rpi-001"]
    assert item["last_seen_ms"] == BASE_TS
    assert item["status"] == "online"
    assert item["last_seen_at"].endswith("Z")
    assert item["reported_version"] == "1.2.3"


def test_gsi1_por_canal():
    table = FakeDevices()
    handler.process(make_event(), table)
    item = table.items["DEVICE#rpi-001"]
    assert item["GSI1PK"] == "CHANNEL#stable"
    assert item["GSI1SK"] == "DEVICE#rpi-001"


def test_sin_canal_no_pone_gsi1():
    table = FakeDevices()
    ev = make_event()
    del ev["release_channel"]
    handler.process(ev, table)
    item = table.items["DEVICE#rpi-001"]
    assert "GSI1PK" not in item


def test_no_retrocede_last_seen():
    table = FakeDevices()
    handler.process(make_event(_ingest_ts_ms=BASE_TS + 1000), table)  # nuevo
    # llega un mensaje VIEJO (p. ej. LWT retenido): no debe pisar el estado nuevo.
    out = handler.process(make_event(status="offline", _ingest_ts_ms=BASE_TS), table)
    assert out == {"ok": True, "stale": True}
    item = table.items["DEVICE#rpi-001"]
    assert item["last_seen_ms"] == BASE_TS + 1000
    assert item["status"] == "online"  # no fue pisado por el offline viejo


def test_avanza_con_mensaje_mas_nuevo():
    table = FakeDevices()
    handler.process(make_event(), table)
    out = handler.process(make_event(status="offline", _ingest_ts_ms=BASE_TS + 5000), table)
    assert out["stale"] is False
    item = table.items["DEVICE#rpi-001"]
    assert item["status"] == "offline"
    assert item["last_seen_ms"] == BASE_TS + 5000


def test_device_id_desde_topic():
    table = FakeDevices()
    ev = {"_device_id_topic": "rpi-099", "status": "online", "_ingest_ts_ms": BASE_TS}
    handler.process(ev, table)
    assert "DEVICE#rpi-099" in table.items


def test_sin_device_id_falla():
    with pytest.raises(ValueError, match="sin device_id"):
        handler.process({"status": "online"}, FakeDevices())
