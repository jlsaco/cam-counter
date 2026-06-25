"""Acceso DynamoDB read-only de la API de flota + cursor opaco de paginación.

Sólo LECTURA (``Query`` / ``GetItem``): el rol de ejecución (módulo ``iam-lambda``, WP03) NO
concede ``PutItem``/``UpdateItem``/``DeleteItem``/``Scan``. Cada listado pagina con un **cursor
opaco** (base64-url del ``LastEvaluatedKey`` de DynamoDB, nunca expuesto en crudo) para que el
cliente nunca construya claves internas.

Convenciones de clave (ver ``keys.py`` y ``CLAUDE.md`` §3):
- ``/devices``                 -> ``Query`` del GSI1 del registro por canal (``CHANNEL#...``).
- ``/devices/{id}``            -> ``GetItem`` por ``PK = DEVICE#{id}``.
- ``/devices/{id}/events``     -> ``Query`` de la partición de una cámara
                                  (``PK = CAM#{site}#{device}#{camera}``) con
                                  ``ScanIndexForward=False`` (más recientes primero).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from keys import CHANNELS, camera_pk, channel_gsi1pk, device_pk

EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "cam-counter-events")
DEVICES_TABLE = os.environ.get("DEVICES_TABLE", "cam-counter-devices")
GSI1_NAME = os.environ.get("GSI1_NAME", "GSI1")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Atributos INTERNOS de clave: no forman parte del contrato público de la API; se eliminan de los
# items devueltos (el cliente pagina con el cursor opaco, no con PK/SK/GSI1*).
_INTERNAL_KEYS = frozenset({"PK", "SK", "GSI1PK", "GSI1SK"})

_resource: Any = None


def _devices_table() -> Any:
    """Tabla DynamoDB de registro de dispositivos (resource cacheado, init perezosa)."""
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _resource.Table(DEVICES_TABLE)


def _events_table() -> Any:
    """Tabla DynamoDB de eventos de cruce (resource cacheado, init perezosa)."""
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _resource.Table(EVENTS_TABLE)


# ───────────────────────── Cursor opaco + saneado de tipos ─────────────────────────


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"no serializable: {type(value).__name__}")


def encode_cursor(token: dict[str, Any] | None) -> str | None:
    """Serializa un token de paginación a base64-url. ``None`` -> ``None`` (no hay más páginas)."""
    if not token:
        return None
    raw = json.dumps(token, default=_json_default, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    """Decodifica un cursor opaco. Lanza ``ValueError`` si está corrupto (=> 400 en el handler)."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        token = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("cursor invalido") from exc
    if not isinstance(token, dict):
        raise ValueError("cursor invalido")
    return token


def _sanitize(item: dict[str, Any]) -> dict[str, Any]:
    """Convierte Decimals a int/float y elimina los atributos internos de clave."""

    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return int(value) if value % 1 == 0 else float(value)
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted(convert(v) for v in value)
        return value

    return {k: convert(v) for k, v in item.items() if k not in _INTERNAL_KEYS}


# ───────────────────────── Consultas read-only ─────────────────────────


def _query_channel(
    table: Any, channel: str, limit: int, start_key: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    kwargs: dict[str, Any] = {
        "IndexName": GSI1_NAME,
        "KeyConditionExpression": Key("GSI1PK").eq(channel_gsi1pk(channel)),
        "Limit": limit,
    }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = table.query(**kwargs)
    return resp.get("Items", []), resp.get("LastEvaluatedKey")


def query_devices(
    channel: str | None, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Lista dispositivos vía ``Query`` del GSI1 por canal.

    Con ``channel`` -> un solo canal, paginado por su ``LastEvaluatedKey``. Sin ``channel`` ->
    recorre los dos canales (``stable``, ``canary``) con un cursor estructurado ``{ci, k}`` que
    recuerda el índice de canal y la clave dentro de él, de modo que ninguna página pierde ni
    duplica dispositivos al cruzar la frontera de canal.
    """
    table = _devices_table()

    if channel is not None:
        token = decode_cursor(cursor)
        start_key = token.get("k") if token else None
        items, lek = _query_channel(table, channel, limit, start_key)
        next_cursor = encode_cursor({"k": lek}) if lek else None
        return [_sanitize(i) for i in items], next_cursor

    token = decode_cursor(cursor) or {}
    ci = int(token.get("ci", 0))
    start_key = token.get("k")
    collected: list[dict[str, Any]] = []
    while ci < len(CHANNELS) and len(collected) < limit:
        need = limit - len(collected)
        items, lek = _query_channel(table, CHANNELS[ci], need, start_key)
        collected.extend(items)
        if lek:
            return [_sanitize(i) for i in collected], encode_cursor({"ci": ci, "k": lek})
        ci += 1
        start_key = None
    next_cursor = encode_cursor({"ci": ci}) if ci < len(CHANNELS) else None
    return [_sanitize(i) for i in collected], next_cursor


def get_device(device_id: str) -> dict[str, Any] | None:
    """Devuelve el item de registro de un dispositivo (``GetItem`` por PK) o ``None``."""
    resp = _devices_table().get_item(Key={"PK": device_pk(device_id)})
    item = resp.get("Item")
    return _sanitize(item) if item else None


def query_events(
    site_id: str, device_id: str, camera_id: str, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Eventos de una cámara, más recientes primero (``ScanIndexForward=False``), paginados."""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(camera_pk(site_id, device_id, camera_id)),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    token = decode_cursor(cursor)
    if token and token.get("k"):
        kwargs["ExclusiveStartKey"] = token["k"]
    resp = _events_table().query(**kwargs)
    items = resp.get("Items", [])
    lek = resp.get("LastEvaluatedKey")
    return [_sanitize(i) for i in items], (encode_cursor({"k": lek}) if lek else None)
