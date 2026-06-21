"""Tests del worker de cloud-sync con FAKES (sin AWS real, sin hardware).

Cubre los comportamientos OBLIGATORIOS (ver PR10 §3):
- subida idempotente de clip (un reintento NO pisa el upload parcial previo,
  ``If-None-Match: *``),
- conditional-put idempotente (duplicado rechazado -> ``synced=1`` sin error),
- heartbeat SÓLO por ``UpdateItem`` y la ASERCIÓN de que el sync NUNCA lee el
  registro para decidir trabajo,
- offline/backlog: con la red caída el conteo persiste en local y NO se sincroniza;
  al reconectar el backlog drena SIN duplicados (mismo ``event_id`` -> dedupe).

Nombres elegidos para casar con ``-k 'idempot or sync_retry or offline or backlog'``.
"""

from __future__ import annotations

from pathlib import Path

from cam_counter_edge import compute_event_id, media_clip_key, ms_to_iso_utc
from cam_counter_edge.store import Store
from cam_counter_edge.sync import AwsClients, CloudSync
from cam_counter_edge.types import CrossingEvent

SITE = "demo-site"
DEVICE = "demo-pi"
CAMERA = "demo-pi-cam0"
MEDIA_BUCKET = "cam-counter-media-950639281773"
EVENTS_TABLE = "cam-counter-events"
DEVICES_TABLE = "cam-counter-devices"


# --------------------------------------------------------------------------- #
# Fakes boto3 (estilo low-level): suficientes para ejercer el worker REAL
# --------------------------------------------------------------------------- #


class _FakeClientError(Exception):
    """Imita ``botocore.exceptions.ClientError`` (lleva ``.response['Error']['Code']``)."""

    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class _FakeNetworkError(Exception):
    """Excepción "transitoria" (red caída): el worker debe detener el batch."""


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls = 0
        self.network_down = False

    def put_object(
        self,
        *,
        Bucket: str,  # noqa: N803 (firma boto3)
        Key: str,  # noqa: N803
        Body: bytes,  # noqa: N803
        ContentType: str | None = None,  # noqa: N803
        IfNoneMatch: str | None = None,  # noqa: N803
    ) -> dict:
        self.put_calls += 1
        if self.network_down:
            raise _FakeNetworkError("s3 unreachable")
        key = (Bucket, Key)
        if IfNoneMatch == "*" and key in self.objects:
            # No pisa un objeto previo (upload parcial/idempotente).
            raise _FakeClientError("PreconditionFailed")
        self.objects[key] = Body
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _FakeClientError("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], dict] = {}
        self.put_calls = 0
        self.update_calls = 0
        self.get_calls = 0
        self.last_update: dict | None = None
        self.network_down = False

    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Item: dict,  # noqa: N803
        ConditionExpression: str | None = None,  # noqa: N803
    ) -> dict:
        self.put_calls += 1
        if self.network_down:
            raise _FakeNetworkError("dynamodb unreachable")
        pk = Item["PK"]["S"]
        sk = Item.get("SK", {}).get("S", "")
        key = (TableName, pk, sk)
        if (
            ConditionExpression
            and "attribute_not_exists" in ConditionExpression
            and key in self.items
        ):
            raise _FakeClientError("ConditionalCheckFailedException")
        self.items[key] = Item
        return {}

    def update_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: dict,  # noqa: N803
        UpdateExpression: str,  # noqa: N803
        ExpressionAttributeNames: dict | None = None,  # noqa: N803
        ExpressionAttributeValues: dict | None = None,  # noqa: N803
    ) -> dict:
        self.update_calls += 1
        if self.network_down:
            raise _FakeNetworkError("dynamodb unreachable")
        self.last_update = {
            "TableName": TableName,
            "Key": Key,
            "UpdateExpression": UpdateExpression,
            "ExpressionAttributeNames": ExpressionAttributeNames,
            "ExpressionAttributeValues": ExpressionAttributeValues,
        }
        return {}

    def get_item(self, *, TableName: str, Key: dict) -> dict:  # noqa: N803
        # El worker NUNCA debería llamar esto para decidir trabajo.
        self.get_calls += 1
        pk = Key["PK"]["S"]
        for (tbl, kpk, _ksk), item in self.items.items():
            if tbl == TableName and kpk == pk:
                return {"Item": item}
        return {}

    def events_count(self, table: str) -> int:
        return sum(1 for (tbl, _pk, _sk) in self.items if tbl == table)


def _make_event(track_id: str, crossing_seq: int, ts_ms: int = 1_700_000_000_000) -> CrossingEvent:
    event_id = compute_event_id(SITE, DEVICE, CAMERA, track_id, crossing_seq)
    return CrossingEvent(
        event_id=event_id,
        site_id=SITE,
        device_id=DEVICE,
        camera_id=CAMERA,
        track_id=track_id,
        crossing_seq=crossing_seq,
        direction="in",
        ts_event_ms=ts_ms,
        ts_event_iso=ms_to_iso_utc(ts_ms),
        positive_label="subieron",
        negative_label="bajaron",
        label="subieron",
        line_version=1,
        confidence=0.9,
        clip_status="pending",
        synced=0,
        created_at=ms_to_iso_utc(ts_ms),
    )


def _worker(store: Store, s3: _FakeS3, dynamo: _FakeDynamo) -> CloudSync:
    return CloudSync(
        store,
        device_id=DEVICE,
        clients=AwsClients(s3=s3, dynamodb=dynamo),
        media_bucket=MEDIA_BUCKET,
        events_table=EVENTS_TABLE,
        devices_table=DEVICES_TABLE,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_idempot_clip_upload_retry_does_not_overwrite_partial(tmp_path: Path) -> None:
    """``sync_retry``: el reintento usa ``If-None-Match`` y NO pisa el clip previo."""
    store = Store(str(tmp_path / "edge.db"))
    s3 = _FakeS3()
    dynamo = _FakeDynamo()
    worker = _worker(store, s3, dynamo)

    event = _make_event("t1", 1)
    store.insert_event(event)
    clip = tmp_path / f"{event.event_id}.mp4"
    clip.write_bytes(b"ORIGINAL-CLIP-BYTES")
    s3_key = media_clip_key(SITE, DEVICE, CAMERA, event.event_id, "mp4", event.ts_event_ms)
    store.enqueue_clip_upload(
        event_id=event.event_id,
        camera_id=CAMERA,
        local_path=str(clip),
        s3_key_planned=s3_key,
    )

    # Primer intento: sube el clip (objeto nuevo).
    outcome1 = worker.sync_event(event)
    assert outcome1.clip_uploaded is True
    assert outcome1.clip_already_present is False
    assert s3.objects[(MEDIA_BUCKET, s3_key)] == b"ORIGINAL-CLIP-BYTES"

    # Reintento del MISMO event_id: If-None-Match rechaza -> NO pisa el objeto.
    clip.write_bytes(b"DIFFERENT-RETRY-BYTES")  # aunque cambie el local
    outcome2 = worker.sync_event(_make_event("t1", 1))
    assert outcome2.clip_uploaded is False
    assert outcome2.clip_already_present is True
    # El objeto en S3 sigue siendo el original (no se pisó el upload previo).
    assert s3.objects[(MEDIA_BUCKET, s3_key)] == b"ORIGINAL-CLIP-BYTES"
    assert s3.put_calls == 2  # se intentó dos veces, pero la 2ª no sobrescribió
    store.close()


def test_idempot_conditional_put_duplicate_marks_synced_without_error(
    tmp_path: Path,
) -> None:
    """``idempot``: el conditional-put rechazado por duplicado marca ``synced=1``."""
    store = Store(str(tmp_path / "edge.db"))
    s3 = _FakeS3()
    dynamo = _FakeDynamo()
    worker = _worker(store, s3, dynamo)

    event = _make_event("t1", 1)
    store.insert_event(event)

    result1 = worker.sync_once()
    assert result1.synced == 1
    assert result1.outcomes[0].put_new is True
    assert dynamo.events_count(EVENTS_TABLE) == 1
    # El evento quedó marcado synced=1 en local.
    assert store.count_unsynced_events() == 0

    # Reintento del MISMO evento: conditional put rechazado, SIN error, synced=1.
    outcome2 = worker.sync_event(_make_event("t1", 1))
    assert outcome2.put_new is False
    assert outcome2.put_duplicate is True
    assert outcome2.marked_synced is True
    # No se duplicó el ítem en DynamoDB.
    assert dynamo.events_count(EVENTS_TABLE) == 1
    store.close()


def test_heartbeat_uses_update_item_only_and_never_reads_registry(
    tmp_path: Path,
) -> None:
    """Heartbeat SÓLO con ``UpdateItem``; el sync NUNCA lee el registro para decidir."""
    store = Store(str(tmp_path / "edge.db"))
    s3 = _FakeS3()
    dynamo = _FakeDynamo()
    worker = _worker(store, s3, dynamo)

    # Drenar eventos + heartbeat: NUNCA debe leer el registro (get_item == 0).
    store.insert_event(_make_event("t1", 1))
    worker.sync_once()
    worker.heartbeat(reported_version="1.2.3", status="online")

    assert dynamo.update_calls == 1
    assert dynamo.last_update is not None
    assert dynamo.last_update["TableName"] == DEVICES_TABLE
    assert dynamo.last_update["Key"]["PK"]["S"] == f"DEVICE#{DEVICE}"
    values = dynamo.last_update["ExpressionAttributeValues"]
    assert values[":rv"] == {"S": "1.2.3"}
    assert values[":st"] == {"S": "online"}
    # NUNCA lee el registro (ni ninguna tabla) para decidir el trabajo.
    assert dynamo.get_calls == 0
    # El heartbeat NO usa PutItem sobre el registro (sólo UpdateItem).
    assert dynamo.events_count(DEVICES_TABLE) == 0
    store.close()


def test_offline_then_online_backlog_drains_without_duplicates(tmp_path: Path) -> None:
    """``offline``/``backlog``: red caída -> conteo persiste; al reconectar drena sin dups."""
    store = Store(str(tmp_path / "edge.db"))
    s3 = _FakeS3()
    dynamo = _FakeDynamo()
    worker = _worker(store, s3, dynamo)

    # El conteo persiste en LOCAL aunque la red esté caída (independiente del sync).
    events = [_make_event(f"t{i}", i) for i in range(1, 4)]
    for ev in events:
        store.insert_event(ev)
    assert store.count_unsynced_events() == 3

    # RED CAÍDA: el sync NO bloquea el conteo y NO pierde eventos (siguen synced=0).
    dynamo.network_down = True
    s3.network_down = True
    result_down = worker.sync_once()
    assert result_down.stopped_offline is True
    assert result_down.synced == 0
    assert store.count_unsynced_events() == 3  # nada se perdió

    # RECONEXIÓN: drena el backlog completo, SIN duplicados.
    dynamo.network_down = False
    s3.network_down = False
    result_up = worker.sync_once()
    assert result_up.synced == 3
    assert store.count_unsynced_events() == 0
    assert dynamo.events_count(EVENTS_TABLE) == 3

    # Re-drenado del MISMO backlog (p.ej. crash antes de marcar): CERO duplicados.
    for ev in events:
        outcome = worker.sync_event(_make_event(ev.track_id, ev.crossing_seq))
        assert outcome.put_duplicate is True
    assert dynamo.events_count(EVENTS_TABLE) == 3  # sigue habiendo 3, no 6
    store.close()
