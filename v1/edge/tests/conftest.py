"""Configuración de pytest del paquete edge: gating ESTRICTO de ``integration_aws``.

La prueba de integración real contra AWS (``test_sync_integration_aws.py``) está
gated por credenciales con DOS modos (spec §3, F8):

- **Sin credenciales utilizables** (CI sin OIDC): la prueba hace ``pytest.skip``.
  NUNCA rompe el build. El guardián de este módulo NO se activa.
- **``CAMCOUNTER_AWS_INTEGRATION=1`` + credenciales resolubles** (entorno del
  runner): la suite ``integration_aws`` DEBE producir al menos un PASS real. Si la
  única salida es SKIP (skip indebido con credenciales), el guardián
  ``pytest_sessionfinish`` convierte el resultado en EXIT-CODE != 0. Así NO se
  puede dar por cumplido el DoD con un skip indebido cuando SÍ hay credenciales.

Este módulo expone además la *fixture* ``aws_integration`` que resuelve los
recursos REALES (bucket de media, tabla de eventos, ARN del rol per-Pi), ASUME el
rol per-Pi (validando el IAM acotado) y garantiza el CLEANUP en el teardown.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Contadores de sesión de la suite integration_aws (los lee el guardián).
_integration = {"collected": 0, "passed": 0, "skipped": 0, "failed": 0}


# ───────────────────────── gating: detección de credenciales ─────────────────


def integration_flag_on() -> bool:
    """¿Está pedido explícitamente el modo integración (``CAMCOUNTER_AWS_INTEGRATION=1``)?"""
    return os.environ.get("CAMCOUNTER_AWS_INTEGRATION") == "1"


def env_credentials_present() -> bool:
    """¿boto3 resuelve credenciales del entorno (cadena estándar)?"""
    try:
        import boto3  # noqa: PLC0415

        return boto3.session.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001 (boto3 ausente o sin credenciales => no utilizables)
        return False


def aws_integration_enabled() -> bool:
    """Integración HABILITADA = flag activo Y credenciales del entorno presentes.

    Cuando está habilitada, un SKIP de la suite cuenta como FALLO (modo estricto).
    Cuando NO lo está (sin flag o sin credenciales), el skip es legítimo.
    """
    return integration_flag_on() and env_credentials_present()


def aws_region() -> str:
    """Región AWS efectiva (env o default ``us-east-1``)."""
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )


def _repo_root() -> Path:
    """Raíz del monorepo (sube desde este archivo buscando ``terraform/``)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "terraform" / "environments" / "prod").is_dir():
            return parent
    return here.parents[3]  # fallback razonable: <root>/v1/edge/tests/conftest.py


def resolve_edge_role_arn() -> str | None:
    """ARN del rol per-Pi: env ``CAMCOUNTER_EDGE_ROLE_ARN`` o output de terraform.

    Fuente canónica (spec §3): output ``edge_role_arn`` de PR04. NUNCA se
    reconstruye desde placeholders de ``site_id``/``device_id``.
    """
    env = os.environ.get("CAMCOUNTER_EDGE_ROLE_ARN")
    if env:
        return env.strip()
    prod = _repo_root() / "terraform" / "environments" / "prod"
    try:
        out = subprocess.run(
            ["terraform", f"-chdir={prod}", "output", "-raw", "edge_role_arn"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    arn = out.stdout.strip()
    return arn or None


# ───────────────────────── contexto de integración (fixture) ─────────────────


@dataclass
class IntegrationContext:
    """Recursos resueltos + clientes para la prueba de integración real.

    ``clients`` opera con las credenciales del ROL per-Pi (valida el IAM acotado).
    ``verify`` opera con las credenciales del ENTORNO (runner) y se usa para el
    read-back y el CLEANUP (el rol per-Pi es write-only: no tiene GetItem en
    eventos ni Delete en ninguna tabla/bucket — por diseño de mínimo privilegio).
    """

    region: str
    media_bucket: str
    events_table: str
    devices_table: str
    role_arn: str | None
    used_assumed_role: bool
    clients: Any
    verify: Any
    _clip_keys: list[str] = field(default_factory=list)
    _event_keys: list[tuple[str, str]] = field(default_factory=list)

    def track_clip(self, key: str) -> None:
        """Registra una clave S3 para limpiarla en el teardown (pase lo que pase)."""
        self._clip_keys.append(key)

    def track_event(self, pk: str, sk: str) -> None:
        """Registra una clave de evento DynamoDB para limpiarla en el teardown."""
        self._event_keys.append((pk, sk))

    def cleanup(self) -> None:
        """Borra TODO lo registrado con las credenciales del entorno (idempotente)."""
        for key in self._clip_keys:
            try:
                self.verify.s3.delete_object(Bucket=self.media_bucket, Key=key)
            except Exception:  # noqa: BLE001 (cleanup best-effort)
                pass
        for pk, sk in self._event_keys:
            try:
                self.verify.dynamodb.delete_item(
                    TableName=self.events_table,
                    Key={"PK": {"S": pk}, "SK": {"S": sk}},
                )
            except Exception:  # noqa: BLE001 (cleanup best-effort)
                pass


@pytest.fixture
def aws_integration() -> Iterator[IntegrationContext]:
    """Resuelve recursos reales, ASUME el rol per-Pi y garantiza el CLEANUP.

    Gating (F8): sin credenciales utilizables -> ``skip``. Con integración
    habilitada: si el ARN no se resuelve o el ``AssumeRole`` falla -> ``fail``
    claro atribuido a configuración de PR04 (F7), salvo que el orquestador active
    el fallback explícito ``CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS=1``.
    """
    if not integration_flag_on() or not env_credentials_present():
        pytest.skip(
            "sin credenciales AWS utilizables; integración real omitida "
            "(define CAMCOUNTER_AWS_INTEGRATION=1 y credenciales para ejecutarla)"
        )

    from cam_counter_edge.sync import (  # noqa: PLC0415
        DEFAULT_DEVICES_TABLE,
        DEFAULT_EVENTS_TABLE,
        DEFAULT_MEDIA_BUCKET,
        build_boto3_clients,
    )

    region = aws_region()
    media_bucket = os.environ.get("CAMCOUNTER_MEDIA_BUCKET", DEFAULT_MEDIA_BUCKET)
    events_table = os.environ.get("CAMCOUNTER_EVENTS_TABLE", DEFAULT_EVENTS_TABLE)
    devices_table = os.environ.get("CAMCOUNTER_DEVICES_TABLE", DEFAULT_DEVICES_TABLE)
    allow_env = os.environ.get("CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS") == "1"

    role_arn = resolve_edge_role_arn()
    used_assumed_role = False
    clients: Any

    import boto3  # noqa: PLC0415

    if role_arn:
        try:
            sts = boto3.client("sts", region_name=region)
            resp = sts.assume_role(
                RoleArn=role_arn, RoleSessionName="pr10-integration"
            )
            c = resp["Credentials"]
            clients = build_boto3_clients(
                region_name=region,
                aws_access_key_id=c["AccessKeyId"],
                aws_secret_access_key=c["SecretAccessKey"],
                aws_session_token=c["SessionToken"],
            )
            used_assumed_role = True
        except Exception as exc:  # noqa: BLE001 (atribución explícita a PR04, F7)
            if not allow_env:
                pytest.fail(
                    "AssumeRole del rol per-Pi FALLÓ con la integración habilitada. "
                    "Atribuible a configuración de PR04 (trust del rol que no lista "
                    "al principal del runner, o permisos STS). NO se degrada en "
                    f"silencio. ARN={role_arn!r}. Detalle: {exc}. "
                    "Para forzar credenciales del entorno (NO valida el IAM acotado) "
                    "activa CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS=1."
                )
            # Fallback EXPLÍCITO del orquestador (TODO: no valida el IAM acotado).
            clients = build_boto3_clients(region_name=region)
    else:
        if not allow_env:
            pytest.fail(
                "No se resolvió el ARN del rol per-Pi (output 'edge_role_arn' de "
                "PR04 ausente y CAMCOUNTER_EDGE_ROLE_ARN no definido). Con la "
                "integración habilitada esto es FALLO (config de PR04), no skip. "
                "Inicializa el backend remoto de terraform o exporta "
                "CAMCOUNTER_EDGE_ROLE_ARN; o activa el fallback explícito "
                "CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS=1 (TODO: no valida IAM)."
            )
        # Fallback EXPLÍCITO del orquestador (TODO: no valida el IAM acotado).
        clients = build_boto3_clients(region_name=region)

    verify = build_boto3_clients(region_name=region)  # credenciales del entorno
    ctx = IntegrationContext(
        region=region,
        media_bucket=media_bucket,
        events_table=events_table,
        devices_table=devices_table,
        role_arn=role_arn,
        used_assumed_role=used_assumed_role,
        clients=clients,
        verify=verify,
    )
    try:
        yield ctx
    finally:
        ctx.cleanup()


def unique_suffix() -> str:
    """Sufijo único por corrida para AISLAR ejecuciones concurrentes del selftest.

    Mantiene el ``event_id`` determinista DENTRO de una corrida (se calcula una
    vez del mismo ``track_id``/``crossing_seq``) y único ENTRE corridas.
    """
    return f"{os.getpid()}-{int(time.time())}"


# ───────────────────────── guardián de modo estricto (F8) ────────────────────


def pytest_configure(config: pytest.Config) -> None:
    """Registra el marker (belt-and-suspenders con pyproject)."""
    config.addinivalue_line(
        "markers",
        "integration_aws: prueba de integración end-to-end contra AWS REAL (PR04)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Cuenta cuántas pruebas ``integration_aws`` forman parte de este run."""
    _integration["collected"] = sum(
        1 for it in items if it.get_closest_marker("integration_aws") is not None
    )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Acumula el resultado FINAL de cada prueba ``integration_aws``."""
    if "integration_aws" not in report.keywords:
        return
    if report.when == "setup" and report.skipped:
        _integration["skipped"] += 1
    elif report.when == "call":
        if report.passed:
            _integration["passed"] += 1
        elif report.failed:
            _integration["failed"] += 1
        elif report.skipped:
            _integration["skipped"] += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Modo ESTRICTO: con integración habilitada, un SKIP indebido = FALLO.

    Si la integración está habilitada (flag + credenciales) y la suite
    ``integration_aws`` formó parte del run pero NO produjo ningún PASS real,
    fuerza un exit-code != 0. Sin credenciales (CI sin OIDC) el guardián no actúa.
    """
    if not aws_integration_enabled():
        return
    if _integration["collected"] == 0:
        return  # la suite integration_aws no se ejecutó en este run
    if _integration["passed"] == 0:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        msg = (
            "MODO ESTRICTO (F8): CAMCOUNTER_AWS_INTEGRATION=1 con credenciales, "
            "pero la suite integration_aws no produjo NINGÚN PASS real "
            f"(passed=0, skipped={_integration['skipped']}, "
            f"failed={_integration['failed']}). Un SKIP indebido cuenta como FALLO."
        )
        if reporter is not None:
            reporter.write_line(msg, red=True)
        session.exitstatus = 1
