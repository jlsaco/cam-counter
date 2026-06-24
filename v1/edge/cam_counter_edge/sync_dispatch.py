"""Despachador del transporte de sync edge -> cloud (``direct`` | ``iot``).

Este módulo es el ÚNICO punto de arranque del sync del proceso de borde: lee
``CAMCOUNTER_SYNC_TRANSPORT`` y delega en el runner del transporte elegido. Es lo que
hace que el **corte del camino directo** (WP16) sea un cambio de UNA variable y
**REVERSIBLE**:

- ``direct`` (por defecto) -> ``sync_runner.main`` (boto3 directo: conditional-put a
  DynamoDB ``cam-counter-events`` + subida de clip a S3 con el rol STS per-Pi). Es el
  camino histórico; el device usa credenciales AWS directas.
- ``iot`` -> ``mqtt_publisher.main`` (publica el ``CrossingEvent`` por MQTT a IoT Core con
  el certificado mTLS del device; la subida de clips usa credenciales temporales del **IoT
  Credential Provider** -role alias de WP04-, NO credenciales AWS directas). La escritura
  en DynamoDB la hace la Lambda de ingesta detrás de la IoT Rule.

**Fail-closed:** un valor de transporte desconocido ABORTA (exit != 0); NUNCA se cae
silenciosamente a ``direct`` ni a ``iot``. Así, una errata en la config no reactiva por
sorpresa el camino directo (ni deja al device mudo). El gate ``CAMCOUNTER_SYNC_ENABLED`` se
respeta igual en ambos transportes (si no está activo, no se arranca nada).

**Guardarraíl (NO-ALCANCE):** ni este módulo ni el modo ``iot`` tocan la identidad de
despliegue del runner MAD (``raspberry`` / ``~/.aws``). El modo ``iot`` deja de usar
credenciales AWS DIRECTAS *del proceso de borde*; la identidad admin con la que MAD aplica
Terraform es independiente y queda intacta (ver ``docs/edge-direct-path-cutover.md``).

NO importa boto3/paho a nivel de módulo: cada runner los importa de forma perezosa. Así
``import cam_counter_edge.sync_dispatch`` (y los tests de enrutado) funcionan sin esas
dependencias.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

__all__ = [
    "TRANSPORT_DIRECT",
    "TRANSPORT_IOT",
    "VALID_TRANSPORTS",
    "main",
    "resolve_transport",
]

_log = logging.getLogger(__name__)

TRANSPORT_DIRECT = "direct"
TRANSPORT_IOT = "iot"
VALID_TRANSPORTS = (TRANSPORT_DIRECT, TRANSPORT_IOT)

_ENABLED_TRUE = {"1", "true", "yes", "on"}


class UnknownTransportError(ValueError):
    """``CAMCOUNTER_SYNC_TRANSPORT`` tiene un valor que no es ``direct`` ni ``iot``."""


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in _ENABLED_TRUE


def resolve_transport(env: Mapping[str, str]) -> str:
    """Normaliza y valida ``CAMCOUNTER_SYNC_TRANSPORT`` (fail-closed).

    Vacío/ausente -> ``direct`` (el camino histórico es el default seguro). Se
    normaliza (trim + minúsculas) y se exige que esté en ``VALID_TRANSPORTS``; si no,
    lanza ``UnknownTransportError`` (NUNCA adivina un transporte).
    """
    raw = (env.get("CAMCOUNTER_SYNC_TRANSPORT") or "").strip().lower() or TRANSPORT_DIRECT
    if raw not in VALID_TRANSPORTS:
        raise UnknownTransportError(
            f"CAMCOUNTER_SYNC_TRANSPORT={raw!r} no válido; "
            f"usa uno de {VALID_TRANSPORTS} (fail-closed: no se arranca ningún transporte)."
        )
    return raw


def main(argv: list[str] | None = None) -> int:
    """Arranca el runner del transporte configurado. Devuelve su código de salida.

    - Si ``CAMCOUNTER_SYNC_ENABLED`` no está activo: no-op (return 0), igual que antes.
    - ``direct`` -> ``sync_runner.main`` (camino histórico, boto3 directo).
    - ``iot``    -> ``mqtt_publisher.main`` (MQTT + IoT Credential Provider; sin creds
      AWS directas; fail-closed de cert/thing al boot lo aplica el propio runner).
    - transporte desconocido -> 2 (fail-closed; no se arranca nada).

    Los imports de los runners son PEREZOSOS para no acoplar este despachador (ni los
    tests de enrutado) a boto3/paho ni a la config de cada transporte.
    """
    logging.basicConfig(level=logging.INFO)

    if not _env_flag(os.environ, "CAMCOUNTER_SYNC_ENABLED"):
        _log.info(
            "cam-counter-sync: CAMCOUNTER_SYNC_ENABLED no está activo; nada que hacer."
        )
        return 0

    try:
        transport = resolve_transport(os.environ)
    except UnknownTransportError as exc:
        _log.error("cam-counter-sync: %s", exc)
        return 2

    if transport == TRANSPORT_IOT:
        _log.info("cam-counter-sync: transporte=iot (MQTT a IoT Core; sin creds AWS directas)")
        from .mqtt_publisher import main as iot_main  # noqa: PLC0415

        return iot_main(argv)

    _log.info("cam-counter-sync: transporte=direct (boto3 directo a DynamoDB/S3)")
    from .sync_runner import main as direct_main  # noqa: PLC0415

    return direct_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
