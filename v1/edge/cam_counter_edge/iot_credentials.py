"""Proveedor de credenciales AWS temporales vía **IoT Credential Provider**.

En modo ``iot`` (ver ``mqtt_publisher``) el proceso de borde **NO** usa credenciales
AWS estáticas ni un rol STS asumido con llaves de larga vida: la subida de clips a S3
usa credenciales **temporales** obtenidas del *IoT Credential Provider* presentando el
**mismo certificado X.509 mTLS** del device (el de MQTT). El flujo es:

1. ``GET https://{endpoint}/role-aliases/{role_alias}/credentials`` con autenticación
   **mTLS** (cert/key/CA del device). El ``role_alias`` (``cam-counter-edge-s3-role-alias``,
   WP04) mapea a un rol IAM de mínimo privilegio (PutObject en el bucket de media).
2. La respuesta trae ``{accessKeyId, secretAccessKey, sessionToken, expiration}``.
3. Con esas credenciales se construye un cliente boto3 S3 (import PEREZOSO).

**Guardarraíl (nota del revisor):** esto NO toca la identidad de despliegue del runner
(``raspberry`` / ``~/.aws``); son credenciales del *device*, derivadas de su certificado.
NO se importan ``boto3``/``ssl`` a nivel de módulo (igual filosofía que ``sync.py``): el
factory por defecto los importa al primer uso, de modo que ``import`` del paquete y los
tests con fakes funcionan sin esas dependencias.

Inyectable para tests: ``IotCredentialProvider`` acepta un ``fetcher`` que devuelve el
dict de credenciales (un fake en CI), evitando red/TLS reales.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .sync import AwsClients

__all__ = [
    "IotCredentialProvider",
    "TemporaryCredentials",
    "credentials_endpoint_url",
    "default_credentials_fetcher",
]

_log = logging.getLogger(__name__)

# Margen de refresco: renueva las credenciales este nº de segundos ANTES de que
# expiren para no usar una credencial recién caducada en plena subida.
_REFRESH_SKEW_S = 300


def credentials_endpoint_url(endpoint: str, role_alias: str) -> str:
    """URL canónica del IoT Credential Provider para un ``role_alias``.

    ``endpoint`` es el host del *credential provider* (``aws iot describe-endpoint
    --endpoint-type iot:CredentialProvider``), SIN esquema. Se normaliza por si
    viniera con ``https://`` o ``/`` sobrante.
    """
    host = endpoint.strip().removeprefix("https://").removeprefix("http://").strip("/")
    alias = role_alias.strip().strip("/")
    if not host or not alias:
        raise ValueError("endpoint y role_alias son obligatorios para el credential provider")
    return f"https://{host}/role-aliases/{alias}/credentials"


@dataclass
class TemporaryCredentials:
    """Credenciales STS temporales devueltas por el IoT Credential Provider."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration_epoch_s: float | None = None

    def is_expiring(self, *, now_s: float, skew_s: int = _REFRESH_SKEW_S) -> bool:
        """``True`` si caducan dentro de ``skew_s`` segundos (o no se sabe)."""
        if self.expiration_epoch_s is None:
            return False
        return now_s >= (self.expiration_epoch_s - skew_s)


def _parse_expiration(raw: Any) -> float | None:
    """Convierte la ``expiration`` ISO-8601 del provider a epoch segundos (best-effort)."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        from datetime import datetime  # noqa: PLC0415

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def default_credentials_fetcher(
    *,
    endpoint: str,
    role_alias: str,
    cert_path: str,
    key_path: str,
    ca_path: str,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Hace el ``GET`` mTLS real al IoT Credential Provider (imports PEREZOSOS).

    Devuelve el dict ``credentials`` crudo (``accessKeyId``/``secretAccessKey``/
    ``sessionToken``/``expiration``). Lanza en error de red/HTTP (lo clasifica el
    caller como transitorio: edge-first, se reintenta luego).
    """
    import ssl  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = credentials_endpoint_url(endpoint, role_alias)
    ctx = ssl.create_default_context(cafile=ca_path)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    req = urllib.request.Request(url, method="GET")  # noqa: S310 (https fijo arriba)
    with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:  # noqa: S310
        body = resp.read()
    payload = json.loads(body)
    creds = payload.get("credentials")
    if not isinstance(creds, dict):
        raise RuntimeError("respuesta del credential provider sin 'credentials'")
    return creds


class IotCredentialProvider:
    """Cachea y refresca credenciales temporales del IoT Credential Provider.

    Construye ``AwsClients`` (S3) con boto3 a partir de las credenciales del device.
    El ``fetcher`` se inyecta en tests (fake) para no tocar red/TLS; por defecto usa
    ``default_credentials_fetcher`` (mTLS real).
    """

    def __init__(
        self,
        *,
        endpoint: str,
        role_alias: str,
        cert_path: str,
        key_path: str,
        ca_path: str,
        region: str,
        fetcher: Callable[..., dict[str, Any]] | None = None,
        client_builder: Callable[[TemporaryCredentials, str], AwsClients] | None = None,
        monotonic: Callable[[], float] = time.time,
    ) -> None:
        self._endpoint = endpoint
        self._role_alias = role_alias
        self._cert_path = cert_path
        self._key_path = key_path
        self._ca_path = ca_path
        self._region = region
        self._fetcher = fetcher or default_credentials_fetcher
        self._client_builder = client_builder or _default_s3_client_builder
        self._now = monotonic
        self._creds: TemporaryCredentials | None = None
        self._clients: AwsClients | None = None

    def fetch(self) -> TemporaryCredentials:
        """Obtiene credenciales frescas del provider (sin caché)."""
        raw = self._fetcher(
            endpoint=self._endpoint,
            role_alias=self._role_alias,
            cert_path=self._cert_path,
            key_path=self._key_path,
            ca_path=self._ca_path,
        )
        return TemporaryCredentials(
            access_key_id=str(raw["accessKeyId"]),
            secret_access_key=str(raw["secretAccessKey"]),
            session_token=str(raw["sessionToken"]),
            expiration_epoch_s=_parse_expiration(raw.get("expiration")),
        )

    def clients(self) -> AwsClients:
        """``AwsClients`` (S3) con credenciales vigentes; refresca si están por caducar."""
        now = self._now()
        if (
            self._clients is None
            or self._creds is None
            or self._creds.is_expiring(now_s=now)
        ):
            self._creds = self.fetch()
            self._clients = self._client_builder(self._creds, self._region)
            _log.info("iot-cred-provider: credenciales temporales renovadas (role_alias)")
        return self._clients


def _default_s3_client_builder(creds: TemporaryCredentials, region: str) -> AwsClients:
    """Construye ``AwsClients`` boto3 con credenciales temporales (import PEREZOSO).

    Sólo S3 es necesario en modo ``iot`` (la escritura en DynamoDB la hace la Lambda
    de ingesta vía la regla de IoT, no el device). El campo ``dynamodb`` queda como
    ``None`` tipado para no construir un cliente que no se usa.
    """
    import boto3  # noqa: PLC0415

    session = boto3.Session(
        aws_access_key_id=creds.access_key_id,
        aws_secret_access_key=creds.secret_access_key,
        aws_session_token=creds.session_token,
        region_name=region,
    )
    return AwsClients(
        s3=session.client("s3", region_name=region),
        dynamodb=None,  # type: ignore[arg-type]  # no se usa en modo iot
    )
