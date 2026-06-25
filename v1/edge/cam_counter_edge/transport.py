"""Selector de transporte de sincronización edge→cloud (``CAMCOUNTER_SYNC_TRANSPORT``).

Flag canónico (``docs/naming-standard.md`` §9.2 y ``docs/iot/edge-dualrun.md``):
``direct`` | ``iot``, default ``direct``.

- ``direct`` (default): camino ACTUAL — ``CloudSync`` (``sync_runner``) escribe DynamoDB/S3
  por boto3 con el rol STS per-Pi. **CERO regresión**: el comportamiento del stack en marcha
  no cambia salvo que se cambie explícitamente el flag.
- ``iot``: sólo MQTT — el ``MqttPublisher`` publica los ``CrossingEvents`` a IoT Core y la
  Lambda de ingesta (WP05) hace el conditional-put; el camino directo queda APAGADO.

**Sub-modo de migración DUAL-RUN** (validar paridad antes de cortar el directo, que es un WP
posterior): se ejecutan AMBOS caminos a la vez. Como el flag SÓLO admite ``direct``|``iot``
(regla del revisor: no se inventa un tercer valor ni valores compuestos), el dual-run NO es
un valor del flag: se activa con el booleano ``CAMCOUNTER_SYNC_DUAL_RUN`` MANTENIENDO
``CAMCOUNTER_SYNC_TRANSPORT=direct`` (el directo sigue siendo la fuente de verdad mientras se
valida MQTT en paralelo). La idempotencia (``event_id`` determinista + conditional put
``PK AND SK`` en la Lambda, ``ts_event_ms`` inmutable) garantiza que coexistir NO duplica.

Matriz de fases:

==================  =================  =========  =======  ===============================
SYNC_TRANSPORT      SYNC_DUAL_RUN      directo    MQTT     fase
==================  =================  =========  =======  ===============================
``direct`` (def)    off (def)          ON         off      actual — cero regresión
``direct``          on                 ON         ON       dual-run (paridad edge↔cloud)
``iot``             (ignorado)         off         ON       solo MQTT (post-validación)
==================  =================  =========  =======  ===============================

Cortar definitivamente el directo (dejar SÓLO ``iot`` como única opción) es trabajo de un WP
posterior; este WP introduce el publicador en paralelo de forma REVERSIBLE.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

__all__ = [
    "DUAL_RUN_ENV",
    "TRANSPORT_DIRECT",
    "TRANSPORT_ENV",
    "TRANSPORT_IOT",
    "VALID_TRANSPORTS",
    "direct_path_enabled",
    "dual_run_enabled",
    "iot_path_enabled",
    "resolve_transport",
]

_log = logging.getLogger(__name__)

TRANSPORT_ENV = "CAMCOUNTER_SYNC_TRANSPORT"
DUAL_RUN_ENV = "CAMCOUNTER_SYNC_DUAL_RUN"

TRANSPORT_DIRECT = "direct"
TRANSPORT_IOT = "iot"
VALID_TRANSPORTS = frozenset({TRANSPORT_DIRECT, TRANSPORT_IOT})

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})


def resolve_transport(env: Mapping[str, str] | None = None) -> str:
    """Devuelve el transporte canónico (``direct``|``iot``) leyendo el entorno.

    Normaliza (``strip``/``lower``). Un valor AUSENTE o VACÍO → ``direct`` (default seguro).
    Un valor DESCONOCIDO (p.ej. ``direct-iam`` de specs antiguas) → ``direct`` con WARNING:
    fail-safe hacia el camino sin regresión en vez de apagar la sincronización por un typo.
    """
    env = os.environ if env is None else env
    raw = env.get(TRANSPORT_ENV, "").strip().lower()
    if not raw:
        return TRANSPORT_DIRECT
    if raw not in VALID_TRANSPORTS:
        _log.warning(
            "%s=%r no es válido (sólo %s); se usa %r (fail-safe sin regresión).",
            TRANSPORT_ENV,
            raw,
            sorted(VALID_TRANSPORTS),
            TRANSPORT_DIRECT,
        )
        return TRANSPORT_DIRECT
    return raw


def dual_run_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``True`` si el sub-modo dual-run está activo (``CAMCOUNTER_SYNC_DUAL_RUN``).

    Sólo tiene efecto con ``CAMCOUNTER_SYNC_TRANSPORT=direct`` (en ``iot`` el directo ya está
    apagado, así que no hay "dual" posible). Ver ``iot_path_enabled``/``direct_path_enabled``.
    """
    env = os.environ if env is None else env
    return env.get(DUAL_RUN_ENV, "").strip().lower() in _TRUE_TOKENS


def direct_path_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``True`` si el camino DIRECTO (``CloudSync``) debe ejecutarse.

    El directo corre en ``direct`` (incluido el dual-run, donde sigue siendo la fuente de
    verdad). En ``iot`` el directo está APAGADO (solo MQTT).
    """
    return resolve_transport(env) == TRANSPORT_DIRECT


def iot_path_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``True`` si el camino MQTT (``MqttPublisher``) debe ejecutarse.

    MQTT corre en ``iot`` (solo MQTT) o en ``direct`` + dual-run (paralelo al directo). En
    ``direct`` sin dual-run, MQTT está APAGADO: arrancar la unidad MQTT con la config por
    defecto es un no-op seguro (cero regresión).
    """
    env = os.environ if env is None else env
    transport = resolve_transport(env)
    if transport == TRANSPORT_IOT:
        return True
    return transport == TRANSPORT_DIRECT and dual_run_enabled(env)
