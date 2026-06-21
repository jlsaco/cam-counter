"""Prueba de INTEGRACIÓN end-to-end contra AWS REAL (recursos de PR04).

Valida el worker de cloud-sync (``cam_counter_edge.sync``) contra el bucket de
media REAL y la tabla de eventos REAL ya desplegados por el runner MAD en PR04 —
NO mocks. Demuestra el **contrato A** (``event_id`` determinista + conditional
put) contra DynamoDB REAL:

  (a) ``PutObject`` real del clip al bucket de media (prefijo del propio Pi),
  (b) conditional-put real del ``CrossingEvent`` a ``cam-counter-events``,
  (c) IDEMPOTENCIA: reintentar el MISMO ``event_id`` NO duplica (conditional put
      RECHAZADO -> éxito idempotente; el ítem sigue siendo único),
  (d) READ-BACK: ``GetItem`` del evento + ``HeadObject`` del clip,
  (e) CLEANUP garantizado (``DeleteItem`` + ``DeleteObject``) en ``finally``.

**IAM acotado (PRIORITARIO):** el worker (ruta de ESCRITURA) opera con credenciales
STS del **rol per-Pi de PR04** (least-privilege), validando que el IAM acotado
PERMITE escribir SÓLO en el propio prefijo de media y la propia partición de
DynamoDB (+ ``UpdateItem`` del propio registro). El read-back y el cleanup usan las
credenciales del ENTORNO (runner): el rol acotado, a propósito, NO concede
``GetItem``/``DeleteItem`` de eventos ni ``DeleteObject`` (mínimo privilegio).

**Gating (F8):** sin credenciales utilizables -> ``pytest.skip`` (CI sin OIDC no se
rompe). Con ``CAMCOUNTER_AWS_INTEGRATION=1`` + credenciales, un SKIP cuenta como
FALLO (lo impone el guardián de ``conftest.py``): el DoD exige un PASS real. Si la
integración está habilitada pero NO se resuelve el ARN del rol o el ``AssumeRole``
FALLA, la prueba FALLA con mensaje claro (config de PR04), nunca degrada en
silencio (salvo fallback explícito del orquestador).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from aws_integration import (
    IntegrationContext,
    allow_env_creds_fallback,
    assume_role_session,
    credentials_available,
    integration_enabled,
    region_name,
    resolve_role_arn,
    resource_names,
    selftest_ids,
)
from cam_counter_edge import compute_event_id, media_clip_key, ms_to_iso_utc
from cam_counter_edge.clip import write_clip
from cam_counter_edge.store import Store
from cam_counter_edge.sync import AwsClients, CloudSync, event_keys
from cam_counter_edge.types import CrossingEvent

pytestmark = pytest.mark.integration_aws


@pytest.fixture
def aws_ctx() -> Iterator[IntegrationContext]:
    """Sesiones AWS para la prueba: ``scoped`` (rol per-Pi) + ``admin`` (entorno).

    Gating estricto F8/F7/F10:
    - sin flag o sin credenciales -> SKIP (no rompe CI sin OIDC),
    - con integración habilitada pero ARN no resuelto / AssumeRole fallido -> FAIL
      claro (config de PR04), nunca SKIP ni degradación silenciosa (salvo fallback
      explícito del orquestador con CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS=1).
    """
    if not integration_enabled():
        pytest.skip(
            "CAMCOUNTER_AWS_INTEGRATION!=1: integración real omitida (CI sin OIDC)"
        )
    if not credentials_available():
        pytest.skip("sin credenciales AWS resolubles; integración real omitida")

    import boto3  # noqa: PLC0415

    region = region_name()
    role_arn = resolve_role_arn()
    used_role = False

    if role_arn:
        try:
            scoped_session = assume_role_session(role_arn, region)
            used_role = True
        except Exception as exc:  # AssumeRole falló con integración habilitada -> FAIL
            if allow_env_creds_fallback():
                # Fallback DOCUMENTADO del orquestador: NO valida el IAM acotado.
                # TODO: rehabilitar el AssumeRole cuando el trust de PR04 liste al
                # principal del runner. Aquí se opera con credenciales del entorno.
                scoped_session = boto3.Session(region_name=region)
            else:
                pytest.fail(
                    f"AssumeRole del rol per-Pi FALLÓ ({role_arn}): {exc!r}. "
                    "Atribuible a la config de PR04 (¿el trust del rol per-Pi lista "
                    "al runner_principal_arn?). NO se degrada en silencio a las "
                    "credenciales del entorno ni a un SKIP. Para forzar el fallback "
                    "documentado: CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS=1."
                )
    else:
        if allow_env_creds_fallback():
            scoped_session = boto3.Session(region_name=region)
        else:
            pytest.fail(
                "No se resolvió el ARN del rol per-Pi (output edge_role_arn de PR04 "
                "ni CAMCOUNTER_EDGE_ROLE_ARN). Atribuible a config de PR04 ausente "
                "(¿backend de terraform inicializado? ¿output presente?). NO se "
                "degrada a SKIP ni a credenciales del entorno sin "
                "CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS=1."
            )

    # Sesión ADMIN (entorno del runner) para read-back y cleanup (el rol acotado no
    # concede GetItem/DeleteItem de eventos ni DeleteObject — least-privilege).
    admin_session = boto3.Session(region_name=region)

    ctx = IntegrationContext(
        region=region,
        role_arn=role_arn,
        used_role=used_role,
        scoped_s3=scoped_session.client("s3", region_name=region),
        scoped_dynamodb=scoped_session.client("dynamodb", region_name=region),
        admin_s3=admin_session.client("s3", region_name=region),
        admin_dynamodb=admin_session.client("dynamodb", region_name=region),
    )
    yield ctx


def _tiny_clip(tmp_path: Path, event_id: str) -> tuple[str, str]:
    """Genera un clip de prueba mínimo REAL con el writer real. Devuelve (ext, ruta)."""
    frames = [
        np.zeros((16, 16, 3), dtype=np.uint8),
        np.full((16, 16, 3), 200, dtype=np.uint8),
        np.zeros((16, 16, 3), dtype=np.uint8),
    ]
    ext, path = write_clip(frames, tmp_path, event_id, fps=5.0)
    return ext, str(path)


def test_sync_event_idempotency_against_real_aws(
    aws_ctx: IntegrationContext, tmp_path: Path
) -> None:
    """(a)-(e) end-to-end contra AWS REAL con el rol per-Pi; idempotencia + cleanup."""
    names = resource_names()
    site_id, device_id, camera_id = selftest_ids()

    # Aislamiento entre corridas concurrentes: track_id único -> event_id único por
    # corrida. DENTRO de la corrida el event_id es DETERMINISTA (idempotencia).
    unique = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    track_id = f"selftest-{unique}"
    crossing_seq = 1
    ts_ms = int(time.time() * 1000)
    event_id = compute_event_id(site_id, device_id, camera_id, track_id, crossing_seq)

    ext, clip_path = _tiny_clip(tmp_path, event_id)
    s3_key = media_clip_key(site_id, device_id, camera_id, event_id, ext, ts_ms)

    event = CrossingEvent(
        event_id=event_id,
        site_id=site_id,
        device_id=device_id,
        camera_id=camera_id,
        track_id=track_id,
        crossing_seq=crossing_seq,
        direction="in",
        ts_event_ms=ts_ms,
        ts_event_iso=ms_to_iso_utc(ts_ms),
        positive_label="subieron",
        negative_label="bajaron",
        label="subieron",
        line_version=1,
        confidence=0.91,
        clip_status="pending",
        synced=0,
        created_at=ms_to_iso_utc(ts_ms),
    )
    keys = event_keys(event)
    ddb_key = {"PK": {"S": keys["PK"]}, "SK": {"S": keys["SK"]}}

    store = Store(str(tmp_path / "edge.db"))
    store.insert_event(event)
    store.enqueue_clip_upload(
        event_id=event_id,
        camera_id=camera_id,
        local_path=clip_path,
        s3_key_planned=s3_key,
    )

    # El WORKER usa las credenciales SCOPED (rol per-Pi): valida el IAM acotado
    # (escribir SÓLO en el propio prefijo de media + la propia partición DynamoDB).
    worker = CloudSync(
        store,
        device_id=device_id,
        clients=AwsClients(s3=aws_ctx.scoped_s3, dynamodb=aws_ctx.scoped_dynamodb),
        media_bucket=names["media_bucket"],
        events_table=names["events_table"],
        devices_table=names["devices_table"],
    )

    try:
        # (a)+(b) primera sincronización: PutObject real + conditional-put NUEVO.
        result1 = worker.sync_once()
        assert result1.synced == 1, f"esperaba 1 sincronizado, got {result1}"
        outcome1 = result1.outcomes[0]
        assert outcome1.put_new is True, "el primer conditional-put debe crear el ítem"
        assert outcome1.clip_uploaded is True, "el clip debe subirse en el primer intento"

        # (d) READ-BACK con credenciales admin (el rol acotado no concede GetItem).
        got = aws_ctx.admin_dynamodb.get_item(
            TableName=names["events_table"], Key=ddb_key, ConsistentRead=True
        )
        assert "Item" in got, "el evento NO quedó escrito en DynamoDB"
        item = got["Item"]
        assert item["event_id"]["S"] == event_id
        assert item["clip_key"]["S"] == s3_key
        assert item["direction"]["S"] == "in"
        head = aws_ctx.admin_s3.head_object(Bucket=names["media_bucket"], Key=s3_key)
        assert int(head["ContentLength"]) > 0, "el clip subido está vacío"

        # (c) IDEMPOTENCIA: reintentar el MISMO event_id NO duplica (put RECHAZADO).
        retry_event = CrossingEvent(
            event_id=event_id,
            site_id=site_id,
            device_id=device_id,
            camera_id=camera_id,
            track_id=track_id,
            crossing_seq=crossing_seq,
            direction="in",
            ts_event_ms=ts_ms,
            ts_event_iso=ms_to_iso_utc(ts_ms),
            positive_label="subieron",
            negative_label="bajaron",
            label="subieron",
            line_version=1,
            confidence=0.91,
            clip_status="pending",
            synced=0,
            created_at=ms_to_iso_utc(ts_ms),
        )
        outcome2 = worker.sync_event(retry_event)
        assert outcome2.put_new is False, "el reintento NO debe crear un ítem nuevo"
        assert outcome2.put_duplicate is True, (
            "el conditional-put debe ser RECHAZADO (ConditionalCheckFailed) y tratarse "
            "como éxito idempotente"
        )
        assert outcome2.marked_synced is True
        # Retry del clip: If-None-Match rechaza -> no se re-subió (idempotente).
        assert outcome2.clip_already_present is True

        # Demostrar que NO hay duplicado: exactamente UN ítem con esa SK (Query admin).
        q = aws_ctx.admin_dynamodb.query(
            TableName=names["events_table"],
            KeyConditionExpression="PK = :pk AND SK = :sk",
            ExpressionAttributeValues={
                ":pk": {"S": keys["PK"]},
                ":sk": {"S": keys["SK"]},
            },
            ConsistentRead=True,
        )
        assert q["Count"] == 1, f"esperaba 1 ítem (sin duplicado), got {q['Count']}"
    finally:
        # (e) CLEANUP SIEMPRE (con credenciales admin): no contamina AWS real.
        try:
            aws_ctx.admin_dynamodb.delete_item(
                TableName=names["events_table"], Key=ddb_key
            )
        except Exception as exc:  # noqa: BLE001 — cleanup best-effort, no enmascarar
            print(f"[cleanup] DeleteItem falló: {exc!r}")
        try:
            aws_ctx.admin_s3.delete_object(Bucket=names["media_bucket"], Key=s3_key)
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] DeleteObject falló: {exc!r}")
        store.close()


def test_scoped_role_heartbeat_update_item_against_real_aws(
    aws_ctx: IntegrationContext, tmp_path: Path
) -> None:
    """El heartbeat (UpdateItem) del registro funciona bajo el rol per-Pi acotado.

    Valida la partición (4) del IAM least-privilege: ``UpdateItem`` SÓLO de la
    propia fila ``DEVICE#{device_id}``. NUNCA lee el registro para decidir. Snapshot
    + restauración/borrado del ítem para no contaminar el registro real.
    """
    names = resource_names()
    _site, device_id, _camera = selftest_ids()
    device_key = {"PK": {"S": f"DEVICE#{device_id}"}}

    # Snapshot previo (admin) para restaurar/borrar en cleanup.
    pre = aws_ctx.admin_dynamodb.get_item(
        TableName=names["devices_table"], Key=device_key, ConsistentRead=True
    )
    existed_before = "Item" in pre

    store = Store(str(tmp_path / "edge.db"))
    worker = CloudSync(
        store,
        device_id=device_id,
        clients=AwsClients(s3=aws_ctx.scoped_s3, dynamodb=aws_ctx.scoped_dynamodb),
        media_bucket=names["media_bucket"],
        events_table=names["events_table"],
        devices_table=names["devices_table"],
    )
    marker = f"selftest-{uuid.uuid4().hex[:8]}"
    try:
        worker.heartbeat(reported_version=marker, status="online")
        got = aws_ctx.admin_dynamodb.get_item(
            TableName=names["devices_table"], Key=device_key, ConsistentRead=True
        )
        assert "Item" in got, "el heartbeat (UpdateItem) no escribió la fila"
        assert got["Item"]["reported_version"]["S"] == marker
        assert got["Item"]["status"]["S"] == "online"
    finally:
        try:
            if existed_before:
                # Restaura el ítem previo tal cual (no contaminar el registro real).
                aws_ctx.admin_dynamodb.put_item(
                    TableName=names["devices_table"], Item=pre["Item"]
                )
            else:
                aws_ctx.admin_dynamodb.delete_item(
                    TableName=names["devices_table"], Key=device_key
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] restauración del registro falló: {exc!r}")
        store.close()
