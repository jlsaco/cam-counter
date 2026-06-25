"""Tests del selector de transporte ``CAMCOUNTER_SYNC_TRANSPORT`` (dual-run reversible).

Verifica la matriz de fases del WP14 (ver ``cam_counter_edge/transport.py``):

==================  =================  =========  =======
SYNC_TRANSPORT      SYNC_DUAL_RUN      directo    MQTT
==================  =================  =========  =======
``direct`` (def)    off (def)          ON         off
``direct``          on                 ON         ON
``iot``             (ignorado)         off         ON
==================  =================  =========  =======

El default (entorno vacío) DEBE ser ``direct`` con MQTT apagado: cero regresión.
"""

from __future__ import annotations

from cam_counter_edge.transport import (
    TRANSPORT_DIRECT,
    TRANSPORT_IOT,
    direct_path_enabled,
    dual_run_enabled,
    iot_path_enabled,
    resolve_transport,
)


def test_default_is_direct_zero_regression() -> None:
    """Entorno SIN el flag -> ``direct``, directo ON, MQTT OFF (cero regresión)."""
    env: dict[str, str] = {}
    assert resolve_transport(env) == TRANSPORT_DIRECT
    assert direct_path_enabled(env) is True
    assert iot_path_enabled(env) is False
    assert dual_run_enabled(env) is False


def test_empty_value_falls_back_to_direct() -> None:
    """Un valor vacío/whitespace cae a ``direct`` (default seguro)."""
    assert resolve_transport({"CAMCOUNTER_SYNC_TRANSPORT": "   "}) == TRANSPORT_DIRECT


def test_iot_only_mqtt() -> None:
    """``iot`` -> sólo MQTT: directo OFF, MQTT ON."""
    env = {"CAMCOUNTER_SYNC_TRANSPORT": "iot"}
    assert resolve_transport(env) == TRANSPORT_IOT
    assert direct_path_enabled(env) is False
    assert iot_path_enabled(env) is True


def test_iot_case_and_whitespace_insensitive() -> None:
    """El valor se normaliza (strip + lower)."""
    env = {"CAMCOUNTER_SYNC_TRANSPORT": "  IoT \n"}
    assert resolve_transport(env) == TRANSPORT_IOT
    assert iot_path_enabled(env) is True


def test_unknown_value_fails_safe_to_direct() -> None:
    """Un valor desconocido (p.ej. el ``direct-iam`` de specs viejas) -> ``direct``.

    Fail-safe: NO apaga la sincronización por un typo; cae al camino sin regresión.
    """
    env = {"CAMCOUNTER_SYNC_TRANSPORT": "direct-iam"}
    assert resolve_transport(env) == TRANSPORT_DIRECT
    assert direct_path_enabled(env) is True
    assert iot_path_enabled(env) is False


def test_dual_run_direct_plus_mqtt() -> None:
    """``direct`` + ``CAMCOUNTER_SYNC_DUAL_RUN=1`` -> AMBOS caminos ON (paridad)."""
    env = {"CAMCOUNTER_SYNC_TRANSPORT": "direct", "CAMCOUNTER_SYNC_DUAL_RUN": "1"}
    assert resolve_transport(env) == TRANSPORT_DIRECT
    assert direct_path_enabled(env) is True  # el directo sigue siendo la fuente de verdad
    assert iot_path_enabled(env) is True  # MQTT corre en paralelo
    assert dual_run_enabled(env) is True


def test_dual_run_ignored_in_iot_mode() -> None:
    """En ``iot`` el directo ya está apagado; dual-run no lo reactiva."""
    env = {"CAMCOUNTER_SYNC_TRANSPORT": "iot", "CAMCOUNTER_SYNC_DUAL_RUN": "1"}
    assert direct_path_enabled(env) is False
    assert iot_path_enabled(env) is True


def test_dual_run_truthy_tokens() -> None:
    """``CAMCOUNTER_SYNC_DUAL_RUN`` acepta los tokens canónicos de verdad."""
    for token in ("1", "true", "TRUE", "yes", "on", " On "):
        env = {"CAMCOUNTER_SYNC_TRANSPORT": "direct", "CAMCOUNTER_SYNC_DUAL_RUN": token}
        assert dual_run_enabled(env) is True, token
    for token in ("0", "false", "no", "off", "", "  "):
        env = {"CAMCOUNTER_SYNC_TRANSPORT": "direct", "CAMCOUNTER_SYNC_DUAL_RUN": token}
        assert dual_run_enabled(env) is False, token
