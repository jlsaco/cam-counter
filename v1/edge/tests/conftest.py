"""conftest del paquete de borde: guardián de modo ESTRICTO de ``integration_aws``.

Gating estricto (F8) de la prueba de integración real contra AWS:

- **Sin credenciales utilizables** (CI sin OIDC): la prueba ``integration_aws``
  hace ``pytest.skip(...)`` y NUNCA rompe el build.
- **Con ``CAMCOUNTER_AWS_INTEGRATION=1`` Y credenciales resolubles** (entorno del
  runner): un SKIP de la suite cuenta como FALLO. Este guardián
  (``pytest_sessionfinish``) convierte "0 passed / N skipped" en exit-code != 0
  cuando hay credenciales, garantizando que el DoD NO se pueda dar por cumplido
  con un skip indebido. Si hubo al menos un PASS real (subió/escribió/leyó/limpió)
  o un fallo explícito, el guardián no interviene (el resultado ya es correcto).
"""

from __future__ import annotations

from typing import Any

import pytest

from aws_integration import strict_mode

# Contadores de la suite integration_aws a lo largo de la sesión.
_counts = {"passed": 0, "failed": 0, "skipped": 0}


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Acumula los resultados de los tests marcados ``integration_aws``."""
    if "integration_aws" not in report.keywords:
        return
    if report.when == "call" and report.passed:
        _counts["passed"] += 1
    elif report.failed:  # error en setup/call/teardown
        _counts["failed"] += 1
    elif report.skipped and report.when in ("setup", "call"):
        _counts["skipped"] += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Modo estricto: con flag + credenciales, un SKIP de la suite es FALLO."""
    if not strict_mode():
        return
    if _counts["passed"] >= 1 or _counts["failed"] >= 1:
        return  # ya hay un PASS real o un fallo explícito: resultado correcto
    if _counts["skipped"] >= 1:
        reporter: Any = session.config.pluginmanager.get_plugin("terminalreporter")
        msg = (
            "GATING ESTRICTO (F8): CAMCOUNTER_AWS_INTEGRATION=1 con credenciales "
            "AWS resolubles, pero la suite integration_aws SÓLO produjo SKIP (0 "
            "passed). El DoD exige un PASS REAL contra AWS (PutObject + "
            "conditional-put + idempotencia + read-back + cleanup). Un skip indebido "
            "con credenciales cuenta como FALLO."
        )
        if reporter is not None:
            reporter.write_sep("=", "integration_aws: SKIP indebido con credenciales", red=True)
            reporter.write_line(msg, red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
