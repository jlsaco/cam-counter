"""Lambda ``cam-counter-device-status`` — upsert de conexión / heartbeat / LWT.

Disparada por las IoT Rules ``cam_counter_device_status`` (online/heartbeat) y
``cam_counter_device_lwt`` (offline por Last-Will-and-Testament), creadas en WP06 —
NO aquí. Hace un ``UpdateItem`` idempotente sobre ``cam-counter-devices``
(``PK = DEVICE#{device_id}``).

Garantía clave (last-writer-wins por tiempo): el guard
``attribute_not_exists(last_seen_ms) OR last_seen_ms <= :now`` evita que un mensaje
LWT retenido viejo PISE un estado más nuevo (``last_seen_ms`` NUNCA retrocede). Si la
condición falla, el mensaje es viejo y se descarta como no-op idempotente.

Mantiene el GSI1 del registro por canal (``GSI1PK = CHANNEL#{release_channel}``,
``GSI1SK = DEVICE#{device_id}``) cuando el evento reporta ``release_channel``.

Cero dependencias externas: sólo boto3/botocore del runtime.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Atributos de telemetría opcionales que el device puede reportar; se escriben sólo si
# vienen en el evento (no se borran si faltan). `status` y `last_seen_*` van aparte.
_OPTIONAL_FIELDS = (
    "reported_version",
    "agent_version",
    "release_channel",
    "line_version",
    "last_update_status",
    "last_good_version",
    "offline_reason",
    "rtsp_ok",
    "hailo_ok",
    "cpu_temp_c",
    "queue_depth",
)

_TABLE = None


def _table():
    """Resource DynamoDB perezoso (testeable: inyección en ``process``)."""
    global _TABLE
    if _TABLE is None:
        _TABLE = boto3.resource("dynamodb").Table(os.environ["DEVICES_TABLE"])
    return _TABLE


def _device_id(event: dict) -> str:
    dev = event.get("device_id") or event.get("_device_id_topic")
    if not dev:
        raise ValueError("evento sin device_id (ni topic(2))")
    return dev


def _now_ms(event: dict) -> int:
    return int(
        event.get("_ingest_ts_ms")
        or event.get("server_ts_ms")
        or int(time.time() * 1000)
    )


def _iso(now_ms: int) -> str:
    return (
        datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_update(event: dict):
    """Construye (Key, UpdateExpression, Names, Values, ConditionExpression).

    Usa ``ExpressionAttributeNames`` para TODOS los atributos (``status`` es palabra
    reservada en DynamoDB; el resto se aliasan por uniformidad/robustez).
    """
    device_id = _device_id(event)
    now_ms = _now_ms(event)

    names = {"#last_seen_ms": "last_seen_ms", "#status": "status", "#last_seen_at": "last_seen_at"}
    values = {
        ":now": now_ms,
        ":st": event.get("status", "online"),
        ":seen_at": _iso(now_ms),
    }
    set_parts = ["#last_seen_ms = :now", "#status = :st", "#last_seen_at = :seen_at"]

    for field in _OPTIONAL_FIELDS:
        if field in event:
            names[f"#{field}"] = field
            values[f":{field}"] = event[field]
            set_parts.append(f"#{field} = :{field}")

    # GSI1 por canal (consistente con device-registry): sólo si se conoce el canal.
    channel = event.get("release_channel")
    if channel:
        names["#GSI1PK"] = "GSI1PK"
        names["#GSI1SK"] = "GSI1SK"
        values[":gsi1pk"] = f"CHANNEL#{channel}"
        values[":gsi1sk"] = f"DEVICE#{device_id}"
        set_parts.append("#GSI1PK = :gsi1pk")
        set_parts.append("#GSI1SK = :gsi1sk")

    return (
        {"PK": f"DEVICE#{device_id}"},
        "SET " + ", ".join(set_parts),
        names,
        values,
        # last-writer-wins por tiempo: no retroceder last_seen_ms.
        "attribute_not_exists(#last_seen_ms) OR #last_seen_ms <= :now",
    )


def process(event: dict, table) -> dict:
    """Núcleo testeable: UpdateItem idempotente con guard de no-retroceso."""
    key, update_expr, names, values, condition = build_update(event)
    try:
        table.update_item(
            Key=key,
            UpdateExpression=update_expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=condition,
        )
        log.info(json.dumps({"msg": "status", "device_id": key["PK"], "stale": False}))
        return {"ok": True, "stale": False}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Mensaje viejo (p. ej. LWT retenido): no pisa un estado más nuevo.
            log.info(json.dumps({"msg": "status", "device_id": key["PK"], "stale": True}))
            return {"ok": True, "stale": True}
        raise


def lambda_handler(event, context):  # noqa: ARG001 (context lo exige el runtime)
    return process(event, _table())
