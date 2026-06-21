"""Prueba de INTEGRACIÓN end-to-end contra AWS REAL (recursos de PR04).

NÚCLEO DEL REQUISITO de PR10. Valida el MISMO worker ``sync.CloudSyncWorker``
(no se reimplementa nada) contra el bucket de media REAL y la tabla de eventos
REAL ya desplegados por el runner MAD en PR04:

  (a) PutObject REAL de un clip pequeño bajo el prefijo del device de selftest,
  (b) conditional-put REAL del CrossingEvent (contrato A: event_id determinista),
  (c) IDEMPOTENCIA: reintentar el MISMO event_id NO duplica (ConditionalCheckFailed
      tratado como éxito idempotente; se ASERTA que no hay segundo item),
  (d) read-back: GetItem del evento + HeadObject del clip,
  (e) CLEANUP garantizado (DeleteItem + DeleteObject) en el teardown de la fixture.

IAM acotado (PRIORITARIO): el worker opera con las credenciales del ROL per-Pi
(``sts:AssumeRole`` del output ``edge_role_arn`` de PR04), validando que el IAM de
mínimo privilegio PERMITE exactamente las escrituras del borde (PutObject en su
prefijo + PutItem en su partición). El read-back y el cleanup usan las
credenciales del ENTORNO (runner): el rol per-Pi es WRITE-ONLY por diseño (no
tiene GetItem en eventos ni Delete en ninguna tabla/bucket).

Gating (F8): sin credenciales utilizables -> SKIP (CI sin OIDC no se rompe). Con
``CAMCOUNTER_AWS_INTEGRATION=1`` + credenciales, el guardián de ``conftest.py``
convierte un SKIP indebido en FALLO (modo estricto). Aislamiento: identificadores
del device de selftest + sufijo único por corrida (event_id determinista DENTRO de
la corrida para poder probar la idempotencia).
"""

from __future__ import annotations

import os
import time

import pytest

from cam_counter_edge.line_counter import compute_event_id, ms_to_iso_utc
from cam_counter_edge.store import Store
from cam_counter_edge.sync import (
    CloudSyncWorker,
    SyncConfig,
    event_pk,
    event_sk,
    media_key_for,
)
from cam_counter_edge.types import CrossingEvent

# Clip de prueba MÍNIMO (cabecera ftyp + relleno): bytes pequeños, no necesita ser
# reproducible; sólo demuestra el PutObject real y el read-back por HeadObject.
_TEST_CLIP = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64

# Identidad de SELFTEST: debe coincidir con el scope del rol per-Pi de PR04
# (``sitio-demo`` / ``rpi-001``) para que el IAM ACOTADO permita las escrituras.
# Usar identificadores ajenos (p.ej. ``selftest``) daría AccessDenied bajo el rol,
# así que el "marcado de selftest" va en el track_id (con sufijo único por corrida).
_SELFTEST_SITE = "sitio-demo"
_SELFTEST_DEVICE = "rpi-001"
_SELFTEST_CAMERA = "rpi-001-cam0"


def _unique_suffix() -> str:
    """Sufijo único por corrida (aísla ejecuciones concurrentes del selftest)."""
    return f"{os.getpid()}-{int(time.time())}"


def _selftest_event() -> CrossingEvent:
    """CrossingEvent de selftest con event_id DETERMINISTA dentro de la corrida."""
    suffix = _unique_suffix()
    track_id = f"selftest-{suffix}"
    # crossing_seq único por corrida (acotado a int) pero estable en la corrida.
    crossing_seq = int(time.time()) % 1_000_000_000
    ts_ms = int(time.time() * 1000)
    event_id = compute_event_id(
        _SELFTEST_SITE, _SELFTEST_DEVICE, _SELFTEST_CAMERA, track_id, crossing_seq
    )
    return CrossingEvent(
        event_id=event_id,
        site_id=_SELFTEST_SITE,
        device_id=_SELFTEST_DEVICE,
        camera_id=_SELFTEST_CAMERA,
        track_id=track_id,
        crossing_seq=crossing_seq,
        direction="in",
        ts_event_ms=ts_ms,
        ts_event_iso=ms_to_iso_utc(ts_ms),
        positive_label="subieron",
        negative_label="bajaron",
        label="subieron",
        line_version=1,
        confidence=0.95,
        clip_status="pending",
        synced=0,
        created_at=ms_to_iso_utc(ts_ms),
    )


@pytest.mark.integration_aws
def test_cloud_sync_against_real_aws(aws_integration, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ctx = aws_integration
    event = _selftest_event()
    clip_key = media_key_for(event)
    pk, sk = event_pk(event), event_sk(event)

    # Registra TODO para el cleanup garantizado (se borra pase lo que pase).
    ctx.track_clip(clip_key)
    ctx.track_event(pk, sk)

    # Store LOCAL: el evento nace ``synced=0`` (como lo dejaría el pipeline).
    store = Store(str(tmp_path / "selftest.db"))
    store.record_event(event)

    # El worker REAL opera con las credenciales del ROL per-Pi (IAM acotado).
    worker = CloudSyncWorker(
        store,
        ctx.clients,
        SyncConfig(
            device_id=_SELFTEST_DEVICE,
            media_bucket=ctx.media_bucket,
            events_table=ctx.events_table,
            devices_table=ctx.devices_table,
        ),
        clip_loader=lambda _e: _TEST_CLIP,
    )

    # (a)+(b) PutObject real + conditional-put real del CrossingEvent.
    first = worker.drain_once()
    assert first.offline is False, "no debería estar offline con credenciales válidas"
    assert first.clips_uploaded == 1, "el clip debió subirse a S3 REAL"
    assert first.written == 1, "el evento debió escribirse (conditional-put aceptado)"
    assert first.synced == 1

    # (c) IDEMPOTENCIA: reintentar el MISMO event_id NO duplica.
    store._conn.execute("UPDATE events SET synced = 0")  # type: ignore[attr-defined]
    second = worker.drain_once()
    assert second.written == 0, "el reintento NO debe escribir un evento nuevo"
    assert second.duplicates == 1, "el conditional-put debe ser rechazado (idempotente)"
    assert second.synced == 1, "el duplicado idempotente marca synced=1 (no es error)"

    # (d) Read-back con credenciales del ENTORNO (el rol per-Pi es write-only).
    got = ctx.verify.dynamodb.get_item(
        TableName=ctx.events_table,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
    )
    assert "Item" in got, "el evento debe existir en DynamoDB REAL (read-back)"
    assert got["Item"]["clip_key"]["S"] == clip_key
    assert "synced" not in got["Item"], "``synced`` es SÓLO-LOCAL, no viaja a la nube"

    head = ctx.verify.s3.head_object(Bucket=ctx.media_bucket, Key=clip_key)
    assert head["ContentLength"] == len(_TEST_CLIP), "el clip debe estar en S3 REAL"

    # ASERCIÓN de NO-duplicado: la partición tiene EXACTAMENTE 1 item con esa SK.
    q = ctx.verify.dynamodb.query(
        TableName=ctx.events_table,
        KeyConditionExpression="PK = :pk AND SK = :sk",
        ExpressionAttributeValues={":pk": {"S": pk}, ":sk": {"S": sk}},
    )
    assert q["Count"] == 1, "idempotencia: un único item para el event_id determinista"

    # El IAM ACOTADO se validó de verdad (operó bajo el rol per-Pi asumido).
    if os.environ.get("CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS") != "1":
        assert ctx.used_assumed_role is True, (
            "la prueba debe operar bajo el rol per-Pi asumido (IAM acotado)"
        )

    store.close()
