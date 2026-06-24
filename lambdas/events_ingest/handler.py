"""Lambda ``cam-counter-events-ingest`` — ingesta idempotente de ``CrossingEvent``.

Disparada por la IoT Rule ``cam_counter_crossing_events`` (creada en WP06, NO aquí),
con SQL que enriquece el payload:

    SELECT *, topic(2) AS _device_id_topic, clientid() AS _client_id,
           timestamp() AS _ingest_ts_ms
    FROM 'cam-counter/+/events/crossing'

Flujo (idempotente y defensivo):
  1. Separa los campos de meta (``_*``) del ``CrossingEvent`` limpio.
  2. Valida el evento limpio VERBATIM contra ``crossing_event.schema.json``.
  3. Anti-spoof (defensa en profundidad): ``topic(2) == device_id`` y, si viene,
     ``clientid == thingName`` esperado.
  4. ``clip_key`` (si viene) acotado al prefijo del propio evento.
  5. Conditional put idempotente con ``attribute_not_exists(PK) AND attribute_not_exists(SK)``.
     ``ConditionalCheckFailedException`` = ÉXITO (dup): se reconcilia ``clip_status``.

Edge-first: esta Lambda es best-effort downstream; el borde cuenta y persiste local
sin depender de ella. Marca ``_ingest_ts_ms`` en el ítem para medir PARIDAD en la
INGESTA (distinguir "MQTT→Lambda funciona" de "sólo el directo"), no en la tabla
(ambos caminos escriben el MISMO ítem idempotente).
"""

from __future__ import annotations

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

from ddb import (
    conditional_put_event,
    is_conditional_check_failed,
    maybe_advance_clip_status,
)
from keys import build_gsi1pk, build_pk, build_sk, looks_like_clip_key
from validation import validate_crossing_event

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Campos de meta que inyecta la IoT Rule: se separan ANTES de validar (el contrato es
# additionalProperties:false). Cualquier clave con prefijo "_" se trata como meta.
_META_PREFIX = "_"

# Campos del contrato CrossingEvent que se PERSISTEN (verbatim). `synced` es SÓLO-local
# (no se sube). Las claves PK/SK/GSI1* y `_ingest_ts_ms` se añaden aparte.
_CONTRACT_FIELDS = (
    "event_id",
    "site_id",
    "device_id",
    "camera_id",
    "track_id",
    "crossing_seq",
    "direction",
    "positive_label",
    "negative_label",
    "label",
    "line_version",
    "ts_event_ms",
    "ts_event_iso",
    "confidence",
    "clip_key",
    "clip_status",
    "schema_version",
    "created_at",
)

_TABLE = None


def _table():
    """Resource DynamoDB perezoso (testeable: monkeypatch o inyección en ``process``)."""
    global _TABLE
    if _TABLE is None:
        _TABLE = boto3.resource("dynamodb").Table(os.environ["EVENTS_TABLE"])
    return _TABLE


def split_meta(event: dict) -> tuple[dict, dict]:
    """Parte el evento en (payload_contrato, meta). La meta son las claves ``_*``."""
    payload, meta = {}, {}
    for key, value in event.items():
        (meta if key.startswith(_META_PREFIX) else payload)[key] = value
    return payload, meta


def _check_anti_spoof(payload: dict, meta: dict) -> None:
    """Defensa en profundidad: topic(2)==device_id y clientid==thingName esperado."""
    device_id = payload["device_id"]
    topic_device = meta.get("_device_id_topic")
    if topic_device not in (None, device_id):
        raise ValueError(
            f"device_id mismatch: topic={topic_device!r} payload={device_id!r}"
        )
    client_id = meta.get("_client_id")
    if client_id is not None:
        expected = f"cam-counter-{payload['site_id']}-{device_id}"
        if client_id != expected:
            raise ValueError(f"clientid spoof: {client_id!r} != {expected!r}")


def build_item(payload: dict, meta: dict) -> dict:
    """Construye el ítem DynamoDB: contrato verbatim + claves derivadas + marca de ingesta."""
    item = {field: payload[field] for field in _CONTRACT_FIELDS if field in payload}
    item["PK"] = build_pk(payload["site_id"], payload["device_id"], payload["camera_id"])
    item["SK"] = build_sk(payload["ts_event_ms"], payload["event_id"])
    item["GSI1PK"] = build_gsi1pk(payload["site_id"])
    item["GSI1SK"] = item["SK"]
    item.setdefault("clip_status", "pending")
    # Marca SÓLO-Lambda: distingue el camino MQTT→Lambda del directo (paridad de ingesta).
    item["_ingest_ts_ms"] = int(meta.get("_ingest_ts_ms") or int(time.time() * 1000))
    return item


def process(event: dict, table) -> dict:
    """Núcleo testeable (sin boto3): valida, anti-spoof, conditional put idempotente."""
    payload, meta = split_meta(event)

    # (1) VERBATIM contra el contrato (slugs, ts_event_ms, schema_version=1, ...).
    validate_crossing_event(payload)

    # (2) anti-spoof.
    _check_anti_spoof(payload, meta)

    # (3) clip_key acotado al prefijo del propio evento (si viene).
    clip_key = payload.get("clip_key")
    if clip_key and not looks_like_clip_key(clip_key, payload):
        raise ValueError(f"clip_key fuera del prefijo del device: {clip_key!r}")

    # (4) conditional put idempotente.
    item = build_item(payload, meta)
    try:
        conditional_put_event(table, item)
        log.info(json.dumps({"msg": "put", "event_id": item["event_id"], "dup": False}))
        return {"ok": True, "dup": False}
    except ClientError as exc:
        if is_conditional_check_failed(exc):
            # Reintento del MISMO event_id: NO es error. Idempotente. Reconcilia clip.
            advanced = maybe_advance_clip_status(table, item)
            log.info(
                json.dumps(
                    {
                        "msg": "dup",
                        "event_id": item["event_id"],
                        "dup": True,
                        "clip_advanced": advanced,
                    }
                )
            )
            return {"ok": True, "dup": True}
        raise  # otros errores -> reintento async / DLQ


def lambda_handler(event, context):  # noqa: ARG001 (context lo exige el runtime)
    return process(event, _table())
