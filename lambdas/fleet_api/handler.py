"""Lambda `cam-counter-fleet-api` — API READ-ONLY de la consola de flota (DynamoDB).

La SPA de la consola NUNCA habla DynamoDB/S3 directo (CLAUDE.md §2): lee la flota a través de
esta API, situada DETRÁS del authorizer JWT Cognito de API Gateway (WP10). Sólo LECTURA: el rol
de ejecución (módulo `iam-lambda`, WP03) concede únicamente `Query`/`GetItem` sobre las tablas
`cam-counter-devices` y `cam-counter-events` (+ su GSI1); NUNCA escribe.

Rutas (HTTP API v2, integración AWS_PROXY, payload 2.0):

    GET /devices[?channel=stable|canary][&limit=N][&cursor=...]
        -> 200 { "devices": [...], "next_cursor": "..."|null }
    GET /devices/{deviceId}
        -> 200 { "device": {...} }                       | 404 si no existe
    GET /devices/{deviceId}/events[?camera=...][&limit=N][&cursor=...]
        -> 200 { "events": [...], "next_cursor": "..."|null, "camera_id": "..." }

PAGINACIÓN: cursor OPACO base64-url (ver `ddb.py`); el cliente nunca construye claves internas.
Los eventos se devuelven más recientes primero (`ScanIndexForward=False`). Como la partición de
eventos es POR CÁMARA (`PK = CAM#{site}#{device}#{camera}`), `/events` resuelve el `site_id` y las
cámaras desde el registro del dispositivo y consulta UNA cámara (la indicada en `?camera=` o, por
defecto, la primera de `camera_ids`). Sin dependencias externas: `boto3` (runtime) + stdlib.
"""

from __future__ import annotations

import json
import os
from typing import Any

import ddb
from keys import CHANNELS, valid_slug

DEFAULT_LIMIT = int(os.environ.get("DEFAULT_LIMIT", "50"))
MAX_LIMIT = int(os.environ.get("MAX_LIMIT", "200"))


class _ApiError(Exception):
    """Error de aplicación con código HTTP explícito (se serializa a `{ "error": ... }`)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _parse_limit(qs: dict[str, str]) -> int:
    raw = qs.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise _ApiError(400, "limit debe ser un entero") from exc
    if limit < 1:
        raise _ApiError(400, "limit debe ser >= 1")
    return min(limit, MAX_LIMIT)


def _list_devices(qs: dict[str, str]) -> dict[str, Any]:
    channel = qs.get("channel")
    if channel is not None and channel not in CHANNELS:
        raise _ApiError(400, f"channel invalido: debe ser uno de {list(CHANNELS)}")
    limit = _parse_limit(qs)
    try:
        devices, next_cursor = ddb.query_devices(channel, limit, qs.get("cursor"))
    except ValueError as exc:
        raise _ApiError(400, str(exc)) from exc
    return _response(200, {"devices": devices, "next_cursor": next_cursor})


def _get_device(device_id: str) -> dict[str, Any]:
    if not valid_slug(device_id):
        raise _ApiError(400, "deviceId invalido (no casa el slug ^[a-z0-9][a-z0-9-]{1,62}$)")
    device = ddb.get_device(device_id)
    if device is None:
        raise _ApiError(404, f"dispositivo no encontrado: {device_id}")
    return _response(200, {"device": device})


def _list_events(device_id: str, qs: dict[str, str]) -> dict[str, Any]:
    if not valid_slug(device_id):
        raise _ApiError(400, "deviceId invalido (no casa el slug ^[a-z0-9][a-z0-9-]{1,62}$)")
    device = ddb.get_device(device_id)
    if device is None:
        raise _ApiError(404, f"dispositivo no encontrado: {device_id}")

    site_id = device.get("site_id")
    camera_ids = device.get("camera_ids") or []
    if not valid_slug(site_id) or not camera_ids:
        raise _ApiError(409, "registro del dispositivo incompleto (site_id/camera_ids)")

    camera_id = qs.get("camera")
    if camera_id is None:
        camera_id = camera_ids[0]
    elif camera_id not in camera_ids:
        raise _ApiError(404, f"camara no registrada en el dispositivo: {camera_id}")

    limit = _parse_limit(qs)
    try:
        events, next_cursor = ddb.query_events(
            site_id, device_id, camera_id, limit, qs.get("cursor")
        )
    except ValueError as exc:
        raise _ApiError(400, str(exc)) from exc
    return _response(
        200, {"events": events, "next_cursor": next_cursor, "camera_id": camera_id}
    )


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
