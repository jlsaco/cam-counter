"""Tests del proveedor de credenciales del IoT Credential Provider (sin red/TLS real).

Cubre:
- la URL canónica del credential provider (normaliza esquema/barras),
- el refresco: se piden credenciales nuevas cuando están por caducar (skew), y se
  cachean mientras siguen vigentes,
- ``fetcher`` y ``client_builder`` inyectables (fakes): ni red ni boto3 reales.

Guardarraíl: estas son credenciales del DEVICE (derivadas de su cert), NO la identidad
de despliegue del runner (``raspberry``/``~/.aws``).
"""

from __future__ import annotations

import pytest

from cam_counter_edge.iot_credentials import (
    IotCredentialProvider,
    TemporaryCredentials,
    credentials_endpoint_url,
)
from cam_counter_edge.sync import AwsClients

ROLE_ALIAS = "cam-counter-edge-s3-role-alias"


def test_endpoint_url_canonical() -> None:
    url = credentials_endpoint_url("abc123.credentials.iot.us-east-1.amazonaws.com", ROLE_ALIAS)
    assert url == (
        "https://abc123.credentials.iot.us-east-1.amazonaws.com/"
        f"role-aliases/{ROLE_ALIAS}/credentials"
    )


def test_endpoint_url_strips_scheme_and_slashes() -> None:
    url = credentials_endpoint_url("https://host.example/", f"/{ROLE_ALIAS}/")
    assert url == f"https://host.example/role-aliases/{ROLE_ALIAS}/credentials"


def test_endpoint_url_requires_both() -> None:
    with pytest.raises(ValueError):
        credentials_endpoint_url("", ROLE_ALIAS)
    with pytest.raises(ValueError):
        credentials_endpoint_url("host", "")


def test_temporary_credentials_is_expiring() -> None:
    creds = TemporaryCredentials("ak", "sk", "tok", expiration_epoch_s=1000.0)
    assert creds.is_expiring(now_s=600.0, skew_s=300) is False  # 1000-300=700 > 600
    assert creds.is_expiring(now_s=750.0, skew_s=300) is True  # 750 >= 700
    # Sin expiración conocida -> no fuerza refresco.
    assert TemporaryCredentials("a", "b", "c").is_expiring(now_s=10**9) is False


def _provider(fetcher, builder, clock) -> IotCredentialProvider:
    return IotCredentialProvider(
        endpoint="host.example",
        role_alias=ROLE_ALIAS,
        cert_path="/dev/null",
        key_path="/dev/null",
        ca_path="/dev/null",
        region="us-east-1",
        fetcher=fetcher,
        client_builder=builder,
        monotonic=clock,
    )


def test_caches_and_refreshes_on_expiry() -> None:
    """Cachea mientras vigente; refresca al acercarse la expiración (skew 300s)."""
    fetched = {"n": 0}
    now = {"t": 0.0}

    def fetcher(**_kwargs):
        fetched["n"] += 1
        # Expira 1000s después del 'ahora' en que se pidió.
        return {
            "accessKeyId": f"AK{fetched['n']}",
            "secretAccessKey": "SK",
            "sessionToken": "TOK",
            "expiration": _iso(now["t"] + 1000.0),
        }

    def builder(creds: TemporaryCredentials, region: str) -> AwsClients:
        return AwsClients(s3=object(), dynamodb=None)  # type: ignore[arg-type]

    prov = _provider(fetcher, builder, lambda: now["t"])

    prov.clients()
    assert fetched["n"] == 1
    # Aún vigente: NO refresca.
    now["t"] = 500.0
    prov.clients()
    assert fetched["n"] == 1
    # Cerca de expirar (1000-300=700 <= 800): refresca.
    now["t"] = 800.0
    prov.clients()
    assert fetched["n"] == 2


def test_fetch_maps_raw_provider_response() -> None:
    def fetcher(**_kwargs):
        return {
            "accessKeyId": "AKIA",
            "secretAccessKey": "secret",
            "sessionToken": "token",
            "expiration": "2030-01-01T00:00:00Z",
        }

    prov = _provider(fetcher, lambda c, r: AwsClients(s3=object(), dynamodb=None), lambda: 0.0)  # type: ignore[arg-type]
    creds = prov.fetch()
    assert creds.access_key_id == "AKIA"
    assert creds.secret_access_key == "secret"
    assert creds.session_token == "token"
    assert creds.expiration_epoch_s is not None


def _iso(epoch_s: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
