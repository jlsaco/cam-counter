"""Ingesta end-to-end con un fake DynamoDB: idempotencia, anti-spoof, clip-link."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import ddb as ddb_mod
import handler as handler_mod
import pytest
from contract import load_schema
from ddb import IDEMPOTENT_CONDITION

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA = load_schema(_REPO_ROOT / "contracts" / "crossing_event.schema.json")
_FULL = json.loads(
    (_REPO_ROOT / "tests" / "contracts" / "examples" / "crossing_event" / "valid" / "full.json")
    .read_text(encoding="utf-8")
)


class FakeDynamo:
    """Fake DynamoDB low-level: dedupe por (PK, SK) con condición PK AND SK."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.put_conditions: list[str] = []
        self.update_calls = 0

    def put_item(self, *, TableName: str, Item: dict, ConditionExpression: str | None = None):  # noqa: N803
        self.put_conditions.append(ConditionExpression or "")
        key = (Item["PK"]["S"], Item["SK"]["S"])
        if ConditionExpression and "attribute_not_exists" in ConditionExpression and key in self.items:
            raise _client_error("ConditionalCheckFailedException")
        self.items[key] = Item
        return {}

    def update_item(  # noqa: N803
        self,
        *,
        TableName: str,
        Key: dict,
        UpdateExpression: str,
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict | None = None,
        ExpressionAttributeValues: dict | None = None,
    ):
        self.update_calls += 1
        key = (Key["PK"]["S"], Key["SK"]["S"])
        item = self.items.get(key)
        if item is None:
            raise _client_error("ResourceNotFoundException")
        if ConditionExpression and "attribute_not_exists(#ck)" in ConditionExpression and "clip_key" in item:
            raise _client_error("ConditionalCheckFailedException")
        item["clip_key"] = ExpressionAttributeValues[":ck"]
        item["clip_status"] = ExpressionAttributeValues[":cs"]
        return {}


def _client_error(code: str) -> Exception:
    exc = RuntimeError(code)
    exc.response = {"Error": {"Code": code}}  # type: ignore[attr-defined]
    return exc


def _proc(ev: dict, fake: FakeDynamo, **kw: Any) -> dict:
    return handler_mod.process_event(
        ev, table_name="t", client=fake, schema=_SCHEMA, now_ms=lambda: 1700000000123, **kw
    )


def test_first_put_creates_with_pk_and_sk_condition() -> None:
    fake = FakeDynamo()
    out = _proc(dict(_FULL), fake)
    assert out["created"] is True and out["duplicate"] is False
    assert fake.put_conditions == [IDEMPOTENT_CONDITION]
    assert IDEMPOTENT_CONDITION == "attribute_not_exists(PK) AND attribute_not_exists(SK)"


def test_ingest_ts_ms_marked_on_item() -> None:
    fake = FakeDynamo()
    _proc(dict(_FULL), fake)
    (item,) = fake.items.values()
    assert item["_ingest_ts_ms"] == {"N": "1700000000123"}


def test_duplicate_is_idempotent_success() -> None:
    fake = FakeDynamo()
    _proc(dict(_FULL), fake)
    out2 = _proc(dict(_FULL), fake)
    assert out2["created"] is False and out2["duplicate"] is True
    assert len(fake.items) == 1  # no duplica


def test_clip_linked_on_duplicate_when_missing() -> None:
    fake = FakeDynamo()
    no_clip = {k: v for k, v in _FULL.items() if k not in ("clip_key", "clip_status")}
    _proc(no_clip, fake)
    (item,) = fake.items.values()
    assert "clip_key" not in item
    out = _proc(dict(_FULL), fake)  # mismo evento, ahora con clip
    assert out["duplicate"] is True and out["clip_linked"] is True
    (item,) = fake.items.values()
    assert item["clip_key"]["S"] == _FULL["clip_key"]


def test_clip_not_relinked_if_present() -> None:
    fake = FakeDynamo()
    _proc(dict(_FULL), fake)  # ya entra con clip
    out = _proc(dict(_FULL), fake)
    assert out["clip_linked"] is False


def test_anti_spoof_event_id_rejected() -> None:
    fake = FakeDynamo()
    bad = dict(_FULL)
    bad["event_id"] = "0" * 40  # válido por pattern pero NO determinista
    with pytest.raises(handler_mod.SpoofError):
        _proc(bad, fake)
    assert not fake.items


def test_anti_spoof_clip_key_rejected() -> None:
    fake = FakeDynamo()
    bad = dict(_FULL)
    bad["clip_key"] = "media/otra/cosa/x/2024/06/20/" + bad["event_id"] + ".mp4"
    with pytest.raises(ValueError):
        _proc(bad, fake)


def test_handler_summary_counts_and_swallows_rejects() -> None:
    handler_mod._SCHEMA = _SCHEMA  # cache para no horneado
    handler_mod._CLIENT = FakeDynamo()
    good = dict(_FULL)
    bad = {"hello": "world"}  # inválido de contrato
    summary = handler_mod.handler([good, bad, dict(_FULL)])
    assert summary["received"] == 3
    assert summary["processed"] == 2
    assert summary["created"] == 1
    assert summary["duplicates"] == 1
    assert summary["rejected"] == 1


def test_transient_put_error_propagates() -> None:
    class Boom(FakeDynamo):
        def put_item(self, **kw: Any):  # noqa: N803
            raise _client_error("ProvisionedThroughputExceededException")

    with pytest.raises(RuntimeError):
        _proc(dict(_FULL), Boom())


def test_default_client_lazy_import_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``default_dynamodb_client`` no se invoca si se inyecta cliente (sin boto3)."""

    def _boom(*_a: Any, **_k: Any):
        raise AssertionError("no debería construir boto3 con cliente inyectado")

    monkeypatch.setattr(ddb_mod, "default_dynamodb_client", _boom)
    _proc(dict(_FULL), FakeDynamo())  # no explota
