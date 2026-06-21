"""Tests del worker cloud-sync (``sync.CloudSyncWorker``) con FAKES de AWS.

Ejercitan en x86 sin red ni AWS real (los clientes boto3 se sustituyen por fakes
que reproducen el *shape* de ``botocore.exceptions.ClientError``):

- subida de clip IDEMPOTENTE y retry-safe (``If-None-Match: *`` no pisa parcial),
- conditional-put IDEMPOTENTE (duplicado rechazado -> ``synced=1`` sin error, sin
  segundo item),
- heartbeat SÓLO por ``UpdateItem`` y aserción de que el worker NUNCA lee el
  registro (ni ``GetItem``/``Query``/``Scan``) para decidir trabajo,
- offline -> online: el conteo persiste en local durante el corte y el backlog
  drena al reconectar SIN duplicados (mismo ``event_id``).
"""

from __future__ import annotations

from typing import Any

from cam_counter_edge.store import Store
from cam_counter_edge.sync import (
    AwsClients,
    CloudSyncWorker,
    SyncConfig,
    media_key_for,
)
from cam_counter_edge.types import CrossingEvent

# ──────────────────────────────── fakes AWS ──────────────────────────────────


class FakeClientError(Exception):
    """Reproduce el *shape* de ``botocore.exceptions.ClientError`` (``response``)."""

    def __init__(self, code: str, message: str = "") -> None:
        self.response = {"Error": {"Code": code, "Message": message}}
        super().__init__(f"{code}: {message}")


class FakeS3:
    """S3 fake: respeta ``If-None-Match: *`` y simula "red caída" con ``down``."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0
        self.down = False

    def put_object(
        self,
        *,
        Bucket: str,  # noqa: N803 (kwargs estilo boto3)
        Key: str,  # noqa: N803
        Body: bytes,  # noqa: N803
        IfNoneMatch: str | None = None,  # noqa: N803
        ContentType: str | None = None,  # noqa: N803
    ) -> dict[str, Any]:
        self.put_calls += 1
        if self.down:
            raise ConnectionError("red caída (S3)")
        if IfNoneMatch == "*" and Key in self.objects:
            # El objeto ya existe: NO se pisa (retry-safe). 412 PreconditionFailed.
            raise FakeClientError("PreconditionFailed", "objeto ya existe")
        self.objects[Key] = Body
        return {"ETag": '"fake"'}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise FakeClientError("404", "no existe")
        return {"ContentLength": len(self.objects[Key])}


class FakeDynamo:
    """DynamoDB fake: conditional-put + spy de métodos (para "nunca leer registro")."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self.down = False

    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Item: dict[str, Any],  # noqa: N803
        ConditionExpression: str | None = None,  # noqa: N803
    ) -> dict[str, Any]:
        self.calls.append("put_item")
        if self.down:
            raise ConnectionError("red caída (DynamoDB)")
        key = (Item["PK"]["S"], Item["SK"]["S"])
        if ConditionExpression and "attribute_not_exists" in ConditionExpression:
            if key in self.items:
                raise FakeClientError(
                    "ConditionalCheckFailedException", "ya existe (idempotente)"
                )
        self.items[key] = Item
        return {}

    def update_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: dict[str, Any],  # noqa: N803
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append("update_item")
        self.updates.append({"TableName": TableName, "Key": Key, **kwargs})
        return {}

    # Métodos de LECTURA: el worker NUNCA debe llamarlos para decidir trabajo.
    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("get_item")
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("query")
        return {"Items": []}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("scan")
        return {"Items": []}


# ──────────────────────────────── helpers ────────────────────────────────────


def _event(
    *,
    track_id: str = "1",
    crossing_seq: int = 1,
    camera_id: str = "rpi-001-cam0",
    ts_event_ms: int = 1_700_000_000_000,
) -> CrossingEvent:
    """``CrossingEvent`` válido para el sync (event_id arbitrario pero estable)."""
    event_id = f"{int(crossing_seq):040d}"  # 40 hex-dígitos válidos (sólo dígitos)
    return CrossingEvent(
        event_id=event_id,
        site_id="sitio-demo",
        device_id="rpi-001",
        camera_id=camera_id,
        track_id=track_id,
        crossing_seq=crossing_seq,
        direction="in",
        ts_event_ms=ts_event_ms,
        ts_event_iso="2023-11-14T22:13:20.000Z",
        positive_label="subieron",
        negative_label="bajaron",
        label="subieron",
        line_version=1,
        confidence=0.9,
        clip_status="pending",
        synced=0,
        created_at="2023-11-14T22:13:20.000Z",
    )


def _worker(
    store: Store,
    s3: FakeS3,
    ddb: FakeDynamo,
    *,
    clip: bytes | None = b"FAKEMP4",
) -> CloudSyncWorker:
    clients = AwsClients(s3=s3, dynamodb=ddb)
    cfg = SyncConfig(device_id="rpi-001")
    loader = (lambda _e: clip) if clip is not None else (lambda _e: None)
    return CloudSyncWorker(store, clients, cfg, clip_loader=loader)


# ──────────────────────────────── tests ──────────────────────────────────────


def test_media_key_format() -> None:
    ev = _event()
    key = media_key_for(ev)
    assert key == (
        f"media/sitio-demo/rpi-001/rpi-001-cam0/2023/11/14/{ev.event_id}.mp4"
    )


def test_event_item_excludes_local_only_synced(tmp_path: Any) -> None:
    store = Store(str(tmp_path / "c.db"))
    s3, ddb = FakeS3(), FakeDynamo()
    worker = _worker(store, s3, ddb)
    ev = _event()
    store.record_event(ev)
    worker.drain_once()
    (item,) = list(ddb.items.values())
    assert "synced" not in item  # ``synced`` es SÓLO-LOCAL, no viaja a la nube
    assert item["PK"]["S"] == "CAM#sitio-demo#rpi-001#rpi-001-cam0"
    assert item["SK"]["S"].startswith("TS#")
    assert item["GSI1PK"]["S"] == "SITE#sitio-demo"
    store.close()


def test_drain_uploads_clip_and_writes_event_then_marks_synced(tmp_path: Any) -> None:
    store = Store(str(tmp_path / "c.db"))
    s3, ddb = FakeS3(), FakeDynamo()
    worker = _worker(store, s3, ddb)
    ev = _event()
    store.record_event(ev)

    result = worker.drain_once()

    assert result.scanned == 1 and result.written == 1 and result.synced == 1
    assert result.clips_uploaded == 1
    assert len(s3.objects) == 1 and len(ddb.items) == 1
    assert store.get_unsynced_events() == []  # marcado synced=1
    # El clip_key del item apunta a la clave canónica de media.
    (item,) = list(ddb.items.values())
    assert item["clip_key"]["S"] == media_key_for(ev)
    assert item["clip_status"]["S"] == "uploaded"
    store.close()


def test_idempotent_conditional_put_duplicate_marks_synced(tmp_path: Any) -> None:
    """Reintentar el MISMO event_id NO duplica: put rechazado -> synced=1 sin error."""
    store = Store(str(tmp_path / "c.db"))
    s3, ddb = FakeS3(), FakeDynamo()
    worker = _worker(store, s3, ddb)
    ev = _event()
    store.record_event(ev)

    first = worker.drain_once()
    assert first.written == 1 and first.duplicates == 0

    # Simula un crash ANTES de marcar synced: el evento vuelve a synced=0.
    store._conn.execute("UPDATE events SET synced = 0")  # type: ignore[attr-defined]
    second = worker.drain_once()

    assert second.scanned == 1
    assert second.written == 0 and second.duplicates == 1  # duplicado idempotente
    assert second.synced == 1  # se marca synced igual (no es error)
    assert len(ddb.items) == 1  # NO se creó un segundo item
    store.close()


def test_sync_retry_safe_clip_not_overwritten(tmp_path: Any) -> None:
    """``If-None-Match: *``: un reintento NO pisa la subida previa del clip."""
    store = Store(str(tmp_path / "c.db"))
    s3, ddb = FakeS3(), FakeDynamo()
    ev = _event()
    store.record_event(ev)

    # 1er pase con bytes "original"; queda en S3.
    worker1 = _worker(store, s3, ddb, clip=b"original")
    worker1.drain_once()
    key = media_key_for(ev)
    assert s3.objects[key] == b"original"

    # Crash antes de marcar synced -> reintento con bytes DISTINTOS.
    store._conn.execute("UPDATE events SET synced = 0")  # type: ignore[attr-defined]
    worker2 = _worker(store, s3, ddb, clip=b"DIFFERENT-do-not-write")
    result = worker2.drain_once()

    assert s3.objects[key] == b"original"  # NO se pisó (retry-safe)
    assert result.synced == 1 and len(ddb.items) == 1
    store.close()


def test_heartbeat_only_updateitem_and_never_reads_registry(tmp_path: Any) -> None:
    store = Store(str(tmp_path / "c.db"))
    s3, ddb = FakeS3(), FakeDynamo()
    worker = _worker(store, s3, ddb)
    for i in range(1, 4):
        store.record_event(_event(track_id=str(i), crossing_seq=i))

    worker.drain_once()
    worker.heartbeat(reported_version="1.2.3", status="online")

    # Heartbeat fue por UpdateItem a DEVICE#rpi-001.
    assert ddb.calls.count("update_item") == 1
    (upd,) = ddb.updates
    assert upd["Key"] == {"PK": {"S": "DEVICE#rpi-001"}}
    # El worker NUNCA leyó el registro (ni ninguna tabla) para decidir trabajo.
    assert "get_item" not in ddb.calls
    assert "query" not in ddb.calls
    assert "scan" not in ddb.calls
    store.close()


def test_offline_then_online_backlog_drains_without_duplicates(tmp_path: Any) -> None:
    """El conteo persiste durante el corte; al reconectar drena SIN duplicados."""
    store = Store(str(tmp_path / "c.db"))
    s3, ddb = FakeS3(), FakeDynamo()
    worker = _worker(store, s3, ddb)

    # La red está caída cuando el pipeline persiste 3 cruces en LOCAL.
    s3.down = True
    ddb.down = True
    for i in range(1, 4):
        assert store.record_event(_event(track_id=str(i), crossing_seq=i)) is True

    # Un pase de sync durante el corte: OFFLINE, nada sincronizado, nada perdido.
    offline_pass = worker.drain_once()
    assert offline_pass.offline is True
    assert offline_pass.synced == 0
    assert len(store.get_unsynced_events()) == 3  # backlog intacto en local

    # Reconecta: el backlog drena completo.
    s3.down = False
    ddb.down = False
    online_pass = worker.drain_once()
    assert online_pass.offline is False
    assert online_pass.synced == 3 and online_pass.written == 3
    assert len(store.get_unsynced_events()) == 0

    # Un pase extra NO reenvía nada (idempotencia) y NO hay items duplicados.
    extra = worker.drain_once()
    assert extra.scanned == 0
    assert len(ddb.items) == 3  # exactamente un item por event_id (sin duplicados)
    store.close()


def test_offline_partial_then_resume_no_duplicate(tmp_path: Any) -> None:
    """Corte a MITAD de un pase: lo ya subido no se reenvía (sin duplicar)."""
    store = Store(str(tmp_path / "c.db"))
    s3 = FakeS3()
    for i in range(1, 4):
        store.record_event(_event(track_id=str(i), crossing_seq=i))

    # Cliente DynamoDB que cae al 2º put (deja 1 evento subido, corta el pase).
    class FlakyDynamo(FakeDynamo):
        def __init__(self) -> None:
            super().__init__()
            self.fail_after = 1

        def put_item(self, **kwargs: Any) -> dict[str, Any]:
            if len([c for c in self.calls if c == "put_item"]) >= self.fail_after:
                self.calls.append("put_item")
                raise ConnectionError("corte a mitad")
            return super().put_item(**kwargs)

    flaky = FlakyDynamo()
    worker = _worker(store, s3, flaky)
    first = worker.drain_once()
    assert first.offline is True
    assert first.synced == 1  # sólo el primero alcanzó a sincronizarse
    assert len(store.get_unsynced_events()) == 2  # los otros 2 siguen pendientes

    # Reanuda con un DynamoDB sano que CONSERVA lo ya escrito (mismo store de items).
    flaky.fail_after = 999  # ya no falla
    second = worker.drain_once()
    assert second.synced == 2 and second.offline is False
    assert len(flaky.items) == 3  # 3 items únicos, sin duplicar el primero
    store.close()
