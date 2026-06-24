"""Lambda `cam-counter-fleet-api` — API read-only de la CONSOLA DE FLOTA cloud.

El frontend (SPA en Amplify, WP13) NUNCA habla DynamoDB/S3 directo: todo pasa por esta API
autenticada con JWT Cognito (authorizer de API Gateway HTTP API v2). Esta función es la cara
de SÓLO LECTURA del registro de dispositivos y del histórico de eventos de cruce.

Rutas (HTTP API v2, payload format 2.0, integración AWS_PROXY):
  GET /devices                       → enumera dispositivos haciendo Query del GSI1 por canal
                                       (CHANNEL#{canary|stable}); NUNCA Scan (least-privilege).
                                       Filtro opcional ?channel=canary|stable.
  GET /devices/{deviceId}            → GetItem PK=DEVICE#{deviceId} del registro de dispositivos.
  GET /devices/{deviceId}/events     → Query de los eventos de cruce de UNA cámara del device
                                       (PK=CAM#{site}#{device}#{camera}) con ScanIndexForward=
                                       false (más recientes primero), Limit y CURSOR OPACO
                                       base64 (esconde la forma de la LastEvaluatedKey).

LEAST-PRIVILEGE: el rol de ejecución (iam-lambda, WP03) sólo concede GetItem/Query/BatchGetItem
sobre las tablas events/devices y sus índices; SIN Scan, SIN escritura. Por eso `GET /devices`
se resuelve con Query del GSI1 por canal (los canales son un conjunto cerrado y pequeño) en vez
de un Scan de la tabla completa.

Sin dependencias externas: sólo boto3 (presente en el runtime de Lambda) + stdlib. La validación
de identificadores reusa el regex canónico de CLAUDE.md §3 (`^[a-z0-9][a-z0-9-]{1,62}$`).
"""

from __future__ import annotations

import base64
import json
import os
import re
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

# ───────────────────────── Configuración por entorno (CAMCOUNTER_* / contexto Lambda) ─────────────────────────
EVENTS_TABLE = os.environ["EVENTS_TABLE"]
DEVICES_TABLE = os.environ["DEVICES_TABLE"]
DEVICES_GSI1 = os.environ.get("DEVICES_GSI1", "GSI1")
# Conjunto CERRADO de canales (contrato device_registry_item: enum canary|stable). Enumerar
# dispositivos = Query del GSI1 por cada canal conocido (sin Scan).
KNOWN_CHANNELS = [c.strip() for c in os.environ.get("KNOWN_CHANNELS", "canary,stable").split(",") if c.strip()]
DEFAULT_PAGE_SIZE = int(os.environ.get("DEFAULT_PAGE_SIZE", "50"))
MAX_PAGE_SIZE = int(os.environ.get("MAX_PAGE_SIZE", "100"))

# Regex canónico de slug (CLAUDE.md §3): valida ANTES de construir claves DynamoDB.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
# Atributos de claves/índices internos de DynamoDB: se eliminan de los items devueltos para no
# filtrar la forma física de las claves al cliente (la API expone el contrato, no el almacén).
_KEY_ATTRS = ("PK", "SK", "GSI1PK", "GSI1SK")

_ddb = boto3.resource("dynamodb")


class _ApiError(Exception):
    """Error con código HTTP asociado (se serializa como {statusCode, body})."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ───────────────────────── Helpers ─────────────────────────
def _json_default(value: Any) -> Any:
    """DynamoDB devuelve números como Decimal: int si es entero, float si no."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"no serializable: {type(value).__name__}")


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    # El CORS lo gestiona API Gateway (cors_configuration del HTTP API); aquí sólo el body.
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def _strip_keys(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in _KEY_ATTRS}


def _require_slug(value: str | None, name: str) -> str:
    if not value or not _SLUG_RE.match(value):
        raise _ApiError(400, f"{name} inválido (debe cumplir ^[a-z0-9][a-z0-9-]{{1,62}}$)")
    return value


def _encode_cursor(last_key: dict[str, Any] | None) -> str | None:
    """LastEvaluatedKey → cursor OPACO base64url (el cliente no debe interpretar su contenido)."""
    if not last_key:
        return None
    raw = json.dumps(last_key, separators=(",", ":"), sort_keys=True, default=_json_default)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        key = json.loads(decoded)
    except Exception as exc:  # noqa: BLE001 — cualquier cursor malformado es 400, no 500.
        raise _ApiError(400, "cursor inválido") from exc
    if not isinstance(key, dict):
        raise _ApiError(400, "cursor inválido")
    return key


def _page_size(qs: dict[str, str]) -> int:
    raw = qs.get("limit")
    if raw is None:
        return DEFAULT_PAGE_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise _ApiError(400, "limit debe ser un entero") from exc
    if value < 1:
        raise _ApiError(400, "limit debe ser >= 1")
    return min(value, MAX_PAGE_SIZE)


# ───────────────────────── Handlers de ruta ─────────────────────────
def _list_devices(qs: dict[str, str]) -> dict[str, Any]:
    """Enumera dispositivos por Query del GSI1 por canal (sin Scan)."""
    channel = qs.get("channel")
    if channel is not None and channel not in KNOWN_CHANNELS:
        raise _ApiError(400, f"channel inválido (esperado uno de {KNOWN_CHANNELS})")
    channels = [channel] if channel else KNOWN_CHANNELS

    table = _ddb.Table(DEVICES_TABLE)
    devices: list[dict[str, Any]] = []
    for ch in channels:
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "IndexName": DEVICES_GSI1,
                "KeyConditionExpression": Key("GSI1PK").eq(f"CHANNEL#{ch}"),
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            resp = table.query(**kwargs)
            devices.extend(_strip_keys(item) for item in resp.get("Items", []))
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
    return _response(200, {"devices": devices, "count": len(devices)})


def _get_device(device_id: str) -> dict[str, Any]:
    device_id = _require_slug(device_id, "deviceId")
    resp = _ddb.Table(DEVICES_TABLE).get_item(Key={"PK": f"DEVICE#{device_id}"})
    item = resp.get("Item")
    if not item:
        raise _ApiError(404, f"device no encontrado: {device_id}")
    return _response(200, {"device": _strip_keys(item)})


def _resolve_camera(device_id: str, qs: dict[str, str]) -> tuple[str, str]:
    """Resuelve (site_id, camera_id) para la Query de eventos.

    Una clave de eventos es CAM#{site}#{device}#{camera}: necesita site_id y camera_id. Se
    toman de los query params si vienen; si no, se leen del item del device (que lleva site_id
    y camera_ids). Con varias cámaras y sin `camera_id` explícito se exige el parámetro (un
    cursor opaco mapea a UNA partición; no se mezclan cámaras).
    """
    qs_site = qs.get("site_id")
    qs_camera = qs.get("camera_id")
    if qs_site and qs_camera:
        return _require_slug(qs_site, "site_id"), _require_slug(qs_camera, "camera_id")

    resp = _ddb.Table(DEVICES_TABLE).get_item(Key={"PK": f"DEVICE#{device_id}"})
    item = resp.get("Item")
    if not item:
        raise _ApiError(404, f"device no encontrado: {device_id}")

    site_id = qs_site or item.get("site_id")
    if not site_id:
        raise _ApiError(400, "site_id no resoluble; pásalo como query param")

    if qs_camera:
        camera_id = qs_camera
    else:
        cameras = item.get("camera_ids") or []
        if len(cameras) == 1:
            camera_id = cameras[0]
        elif not cameras:
            raise _ApiError(400, "el device no declara camera_ids; pasa camera_id como query param")
        else:
            raise _ApiError(400, f"el device tiene {len(cameras)} cámaras; pasa camera_id (una de {cameras})")
    return _require_slug(site_id, "site_id"), _require_slug(camera_id, "camera_id")


def _list_events(device_id: str, qs: dict[str, str]) -> dict[str, Any]:
    device_id = _require_slug(device_id, "deviceId")
    site_id, camera_id = _resolve_camera(device_id, qs)
    limit = _page_size(qs)
    start_key = _decode_cursor(qs.get("cursor"))

    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"CAM#{site_id}#{device_id}#{camera_id}"),
        "ScanIndexForward": False,  # más recientes primero (SK = TS#{ms}#{event_id}).
        "Limit": limit,
    }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = _ddb.Table(EVENTS_TABLE).query(**kwargs)

    events = [_strip_keys(item) for item in resp.get("Items", [])]
    return _response(
        200,
        {
            "events": events,
            "count": len(events),
            "next_cursor": _encode_cursor(resp.get("LastEvaluatedKey")),
        },
    )


# ───────────────────────── Dispatch ─────────────────────────
def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    route_key = event.get("routeKey", "")
    path_params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}

    try:
        if route_key == "GET /devices":
            return _list_devices(qs)
        if route_key == "GET /devices/{deviceId}":
            return _get_device(path_params.get("deviceId", ""))
        if route_key == "GET /devices/{deviceId}/events":
            return _list_events(path_params.get("deviceId", ""), qs)
        raise _ApiError(404, f"ruta no encontrada: {route_key}")
    except _ApiError as err:
        return _response(err.status, {"error": err.message})
