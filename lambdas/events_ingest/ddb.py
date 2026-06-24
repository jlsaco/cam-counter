"""Acceso DynamoDB de la ingesta: conditional put idempotente + enlace de clip.

La idempotencia del contrato A (``CrossingEvent``) descansa en DOS piezas:

1. ``event_id`` DETERMINISTA (sha1 de la tupla de identidad) → PK/SK estables.
2. Conditional put ``attribute_not_exists(PK) AND attribute_not_exists(SK)``:
   un reintento del MISMO evento (o el dual-write directo+MQTT) NO duplica; el
   ``ConditionalCheckFailedException`` es ÉXITO idempotente, no error.

La condición usa **PK AND SK** (no sólo PK) para casar EXACTAMENTE con la del
camino de borde (``cam_counter_edge.sync``): así ambos caminos compiten por la
misma escritura sin que uno «gane» por una condición más laxa.

Marca ``_ingest_ts_ms`` (epoch ms del momento de ingesta de la Lambda) SÓLO en el
item creado por este camino: es la señal que permite medir paridad EN LA INGESTA
(distinguir «MQTT funciona» de «sólo el directo funciona»), ya que el camino
directo edge→cloud no la escribe.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from keys import build_keys

__all__ = [
    "DynamoLike",
    "default_dynamodb_client",
    "is_conditional_check_failed",
    "put_event_idempotent",
    "serialize_event_item",
    "try_link_clip",
]

# Condición de idempotencia COMPARTIDA con el borde (verbatim). No cambiar sin
# alinear ``cam_counter_edge.sync`` (divergiría la dedupe).
IDEMPOTENT_CONDITION = "attribute_not_exists(PK) AND attribute_not_exists(SK)"

# Atributos del contrato CrossingEvent que se persisten en la nube. ``synced`` es
# SÓLO-local (no se sube). Mapea cada campo a su tipo DynamoDB low-level.
_STRING_FIELDS = (
    "event_id",
    "site_id",
    "device_id",
    "camera_id",
    "direction",
    "positive_label",
    "negative_label",
    "label",
    "ts_event_iso",
    "clip_key",
    "clip_status",
    "created_at",
)
_INT_FIELDS = ("crossing_seq", "line_version", "ts_event_ms", "schema_version")
_NUM_FIELDS = ("confidence",)


class DynamoLike(Protocol):
    """Subconjunto de la API boto3 DynamoDB que usa la ingesta."""

    def put_item(self, **kwargs: Any) -> Any: ...

    def update_item(self, **kwargs: Any) -> Any: ...


def default_dynamodb_client(region: str = "us-east-1") -> DynamoLike:
    """Cliente boto3 DynamoDB (import PEREZOSO; boto3 lo provee el runtime Lambda)."""
    import boto3  # noqa: PLC0415 (perezoso: boto3 viene del runtime, no se vendoriza)

    return boto3.client("dynamodb", region_name=region)


def _error_code(exc: BaseException) -> str | None:
    """Extrae ``Error.Code`` de una excepción estilo botocore ``ClientError``."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


def is_conditional_check_failed(exc: BaseException) -> bool:
    """``True`` si ``exc`` es un conditional put rechazado (duplicado idempotente)."""
    return _error_code(exc) == "ConditionalCheckFailedException"


def serialize_event_item(event: dict, ingest_ts_ms: int) -> dict[str, dict[str, str]]:
    """Serializa el evento al formato de atributos DynamoDB low-level.

    Incluye PK/SK + GSI1 y ``_ingest_ts_ms`` (marca de paridad SÓLO-Lambda). Omite
    los campos opcionales ausentes (item compacto, contrato limpio).
    """
    keys = build_keys(event)
    item: dict[str, dict[str, str]] = {
        "PK": {"S": keys["PK"]},
        "SK": {"S": keys["SK"]},
        "GSI1PK": {"S": keys["GSI1PK"]},
        "GSI1SK": {"S": keys["GSI1SK"]},
        # Marca de ingesta MQTT (paridad medida EN LA INGESTA, no en la tabla).
        "_ingest_ts_ms": {"N": str(int(ingest_ts_ms))},
    }
    for field in _STRING_FIELDS:
        value = event.get(field)
        if value is not None:
            item[field] = {"S": str(value)}
    for field in _INT_FIELDS:
        value = event.get(field)
        if value is not None:
            item[field] = {"N": str(int(value))}
    for field in _NUM_FIELDS:
        value = event.get(field)
        if value is not None:
            item[field] = {"N": repr(float(value))}
    return item


def put_event_idempotent(
    client: DynamoLike, table_name: str, item: dict[str, dict[str, str]]
) -> bool:
    """Conditional put del evento. ``True`` si creó item NUEVO, ``False`` si duplicado.

    El duplicado (``ConditionalCheckFailedException``) NO es error: es la
    idempotencia del contrato. Cualquier otra excepción se PROPAGA (la Lambda la
    deja fallar → reintento/DLQ).
    """
    try:
        client.put_item(
            TableName=table_name,
            Item=item,
            ConditionExpression=IDEMPOTENT_CONDITION,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — se reclasifica abajo
        if is_conditional_check_failed(exc):
            return False
        raise


def try_link_clip(
    client: DynamoLike,
    table_name: str,
    keys: dict[str, str],
    clip_key: str,
    clip_status: str,
) -> bool:
    """Enlaza ``clip_key``/``clip_status`` a un item EXISTENTE sólo si le falta.

    Se usa cuando el put falló por duplicado pero el payload trae media: rellena
    ``clip_key`` con ``attribute_not_exists(clip_key)`` (no pisa un enlace previo;
    idempotente). Si el item ya tenía clip, el ``ConditionalCheckFailedException``
    se traga (nada que hacer). ``True`` si enlazó.
    """
    try:
        client.update_item(
            TableName=table_name,
            Key={"PK": {"S": keys["PK"]}, "SK": {"S": keys["SK"]}},
            UpdateExpression="SET #ck = :ck, #cs = :cs",
            ConditionExpression="attribute_not_exists(#ck)",
            ExpressionAttributeNames={"#ck": "clip_key", "#cs": "clip_status"},
            ExpressionAttributeValues={
                ":ck": {"S": clip_key},
                ":cs": {"S": clip_status},
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001
        if is_conditional_check_failed(exc):
            return False  # ya tenía clip: nada que enlazar (idempotente)
        raise


# Factory por defecto inyectable (tests pasan un fake; runtime usa boto3 real).
ClientFactory = Callable[[], DynamoLike]
