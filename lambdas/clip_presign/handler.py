"""Lambda `cam-counter-clip-presign` — firma URLs GET de descarga de media (clips/gifs/snaps).

La SPA de la consola de flota necesita mostrar el clip de un evento sin recibir credenciales AWS
ni acceso directo al bucket privado de media. Esta función, tras el authorizer JWT Cognito de
API Gateway, devuelve una **presigned URL GET de corta vida (TTL 300s)** para una `key` del
prefijo `media/`.

Ruta (HTTP API v2, AWS_PROXY):
  GET /clips/url?key=media/{site}/{device}/{camera}/{yyyy}/{mm}/{dd}/{event_id}.{ext}
                                     → 200 { url, key, expires_in }.

SEGURIDAD DE LA KEY (path traversal): la `key` se valida contra un regex ANCLADO a `media/`
(convención de claves de CLAUDE.md §7) y se RECHAZA cualquier `..`, barra inicial, doble barra o
backslash. El rol de ejecución (iam-lambda, WP03) sólo concede `s3:GetObject` sobre
`{bucket}/media/*` y exige TLS, así que firmar fuera de ese prefijo no daría acceso; aun así se
valida en la app para fallar pronto (400) y no firmar URLs hacia claves arbitrarias.

Firmar NO contacta S3 (operación local con SigV4); por eso no se comprueba la existencia del
objeto (evita una llamada extra y latencia). Una URL hacia una key inexistente devolvería 404 de
S3 al usarla. Sin dependencias externas: boto3 (runtime) + stdlib.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import boto3
from botocore.config import Config

MEDIA_BUCKET = os.environ["MEDIA_BUCKET"]
PRESIGN_TTL = int(os.environ.get("PRESIGN_TTL", "300"))
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Regex ANCLADO al prefijo `media/`: segmentos seguros + extensión final. NO admite `..`, barra
# inicial/doble ni backslash (se comprueban además explícitamente abajo). Acota a la convención
# de claves de media de CLAUDE.md §7.
_KEY_RE = re.compile(r"^media/[A-Za-z0-9][A-Za-z0-9._/-]*\.[A-Za-z0-9]+$")

# SigV4 explícito (s3v4) + endpoint regional: las presigned GET deben firmarse con SigV4.
_s3 = boto3.client("s3", region_name=AWS_REGION, config=Config(signature_version="s3v4"))


class _ApiError(Exception):
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


def _valid_key(key: str | None) -> bool:
    if not key:
        return False
    if ".." in key or "\\" in key or "//" in key:
        return False
    if key.startswith("/"):
        return False
    return bool(_KEY_RE.match(key))


def _presign(qs: dict[str, str]) -> dict[str, Any]:
    key = qs.get("key")
    if not _valid_key(key):
        raise _ApiError(400, "key inválida: debe anclarse a 'media/' y no contener '..'")
    url = _s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": MEDIA_BUCKET, "Key": key},
        ExpiresIn=PRESIGN_TTL,
    )
    return _response(200, {"url": url, "key": key, "expires_in": PRESIGN_TTL})


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    route_key = event.get("routeKey", "")
    qs = event.get("queryStringParameters") or {}
    try:
        if route_key == "GET /clips/url":
            return _presign(qs)
        raise _ApiError(404, f"ruta no encontrada: {route_key}")
    except _ApiError as err:
        return _response(err.status, {"error": err.message})
