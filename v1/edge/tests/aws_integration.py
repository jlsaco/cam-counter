"""Helpers compartidos del gating de la prueba de integración REAL contra AWS.

Centraliza la lógica de:
- detección de la integración habilitada (``CAMCOUNTER_AWS_INTEGRATION=1``) y de
  credenciales utilizables (resolubles por boto3/botocore),
- resolución de nombres de recursos (bucket de media, tablas) y del ARN del rol
  per-Pi (output ``edge_role_arn`` de PR04 vía terraform, o
  ``CAMCOUNTER_EDGE_ROLE_ARN``),
- construcción de sesiones boto3 asumiendo el rol per-Pi (least-privilege) y una
  sesión "admin" con las credenciales del entorno (para read-back y cleanup, que
  el rol acotado NO permite a propósito).

Lo usan tanto ``conftest.py`` (guardián de modo estricto, F8) como
``test_sync_integration_aws.py`` para hablar el MISMO gating.

IMPORTANTE — IDENTIFICADORES DE SELFTEST: el rol per-Pi de PR04 está acotado por
``site_id``/``device_id`` (least-privilege: sólo su propio prefijo de media y su
partición de DynamoDB). Por eso los identificadores de la prueba deben COINCIDIR
con el alcance del rol asumido — de lo contrario el IAM acotado DENIEGA la
escritura (que es justo lo que se valida). Los defaults
(``sitio-demo``/``rpi-001``) son los placeholders del PRIMER Pi en
``terraform/environments/prod/main.tf``; se pueden override por entorno.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Flags del orquestador.
FLAG_INTEGRATION = "CAMCOUNTER_AWS_INTEGRATION"
FLAG_ALLOW_ENV_CREDS = "CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS"

# Defaults canónicos (coherentes con CLAUDE.md §7/§8 y PR04). Configuración
# PÚBLICA por convención de nombre, NO secretos.
DEFAULT_MEDIA_BUCKET = "cam-counter-media-950639281773"
DEFAULT_EVENTS_TABLE = "cam-counter-events"
DEFAULT_DEVICES_TABLE = "cam-counter-devices"
DEFAULT_REGION = "us-east-1"

# Identificadores de selftest = alcance del rol per-Pi de PR04 (deben coincidir
# para que el IAM acotado permita la escritura). Override por entorno.
DEFAULT_SELFTEST_SITE_ID = "sitio-demo"
DEFAULT_SELFTEST_DEVICE_ID = "rpi-001"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def integration_enabled() -> bool:
    """``True`` si el orquestador activó la integración real (flag)."""
    return _flag(FLAG_INTEGRATION)


def allow_env_creds_fallback() -> bool:
    """``True`` si se permite el fallback DOCUMENTADO a credenciales del entorno.

    Sólo cuando el orquestador lo activa explícitamente (no valida el IAM acotado).
    """
    return _flag(FLAG_ALLOW_ENV_CREDS)


def credentials_available() -> bool:
    """``True`` si boto3/botocore resuelve credenciales AWS utilizables."""
    try:
        import botocore.session  # noqa: PLC0415

        return botocore.session.get_session().get_credentials() is not None
    except Exception:
        return False


def strict_mode() -> bool:
    """Modo ESTRICTO del runner (F8): flag activo Y credenciales resolubles.

    En este modo un SKIP de la suite ``integration_aws`` cuenta como FALLO (lo
    impone el guardián de ``conftest.py``): con credenciales, el DoD EXIGE un PASS
    real (subió/escribió/leyó/limpió de verdad).
    """
    return integration_enabled() and credentials_available()


def region_name() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )


def _repo_root() -> Path:
    # tests/aws_integration.py -> tests -> edge -> v1 -> <repo>
    return Path(__file__).resolve().parents[3]


def terraform_output(name: str) -> str | None:
    """Lee un output de ``terraform/environments/prod`` (read-only; degrada limpio).

    Devuelve ``None`` si terraform no está, el backend no está inicializado o el
    output no existe — el caller decide el fallback (env/defaults o FALLO).
    """
    prod = _repo_root() / "terraform" / "environments" / "prod"
    if not prod.is_dir():
        return None
    try:
        out = subprocess.run(
            ["terraform", f"-chdir={prod}", "output", "-raw", name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def resource_names() -> dict[str, str]:
    """Nombres de recursos AWS: entorno -> terraform output -> default canónico."""
    return {
        "media_bucket": (
            os.environ.get("CAMCOUNTER_MEDIA_BUCKET")
            or terraform_output("media_bucket_name")
            or DEFAULT_MEDIA_BUCKET
        ),
        "events_table": (
            os.environ.get("CAMCOUNTER_EVENTS_TABLE")
            or terraform_output("events_table_name")
            or DEFAULT_EVENTS_TABLE
        ),
        "devices_table": (
            os.environ.get("CAMCOUNTER_DEVICES_TABLE")
            or terraform_output("devices_table_name")
            or DEFAULT_DEVICES_TABLE
        ),
    }


def resolve_role_arn() -> str | None:
    """ARN del rol per-Pi: ``CAMCOUNTER_EDGE_ROLE_ARN`` -> output ``edge_role_arn``.

    NUNCA se reconstruye desde placeholders de ``site_id``/``device_id``. Si no se
    resuelve, devuelve ``None`` y el caller (con integración habilitada) FALLA con
    mensaje claro atribuido a la config de PR04 (no degrada en silencio).
    """
    return os.environ.get("CAMCOUNTER_EDGE_ROLE_ARN") or terraform_output("edge_role_arn")


def selftest_ids() -> tuple[str, str, str]:
    """``(site_id, device_id, camera_id)`` de selftest (alcance del rol per-Pi).

    Override por ``CAMCOUNTER_SELFTEST_SITE_ID``/``..._DEVICE_ID``. El ``camera_id``
    se construye con la convención ``{device_id}-cam0`` (slug global único).
    """
    from cam_counter_edge import make_camera_id  # noqa: PLC0415

    site_id = os.environ.get("CAMCOUNTER_SELFTEST_SITE_ID", DEFAULT_SELFTEST_SITE_ID)
    device_id = os.environ.get("CAMCOUNTER_SELFTEST_DEVICE_ID", DEFAULT_SELFTEST_DEVICE_ID)
    camera_id = make_camera_id(device_id, 0)
    return site_id, device_id, camera_id


@dataclass
class IntegrationContext:
    """Sesiones/clientes para la prueba: ``scoped`` (rol per-Pi) + ``admin`` (entorno).

    - ``scoped_*``: credenciales STS del rol per-Pi (least-privilege). Las usa el
      WORKER para validar que el IAM acotado PERMITE la escritura (PutObject al
      propio prefijo + PutItem a la propia partición + UpdateItem del propio
      registro). El rol NO permite GetItem/DeleteItem de eventos ni DeleteObject:
      eso es intencional (mínimo privilegio).
    - ``admin_*``: credenciales del entorno (runner). Las usa la prueba para el
      READ-BACK (GetItem/HeadObject/Query) y el CLEANUP (DeleteItem/DeleteObject),
      operaciones que el rol acotado deliberadamente no concede.
    """

    region: str
    role_arn: str | None
    used_role: bool
    scoped_s3: Any
    scoped_dynamodb: Any
    admin_s3: Any
    admin_dynamodb: Any


def assume_role_session(role_arn: str, region: str) -> Any:
    """Asume el rol per-Pi vía STS y devuelve una ``boto3.Session`` temporal."""
    import boto3  # noqa: PLC0415

    sts = boto3.client("sts", region_name=region)
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="cam-counter-pr10-selftest",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
