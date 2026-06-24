"""Pruebas del despachador de transporte de sync (``cam_counter_edge.sync_dispatch``).

Ejercita en CI x86 (sin boto3/paho/AWS) el ENRUTADO del corte del camino directo
(WP16): la resolución fail-closed de ``CAMCOUNTER_SYNC_TRANSPORT`` y que ``main``
delegue en el runner correcto sólo cuando procede. Los runners reales se monkeypatchean
(no se toca red ni AWS). También cubre el fail-closed de material mTLS del modo ``iot``.
"""

from __future__ import annotations

import pytest

from cam_counter_edge import mqtt_publisher, sync_dispatch
from cam_counter_edge.mqtt_publisher import missing_iot_cert_material
from cam_counter_edge.sync_dispatch import (
    TRANSPORT_DIRECT,
    TRANSPORT_IOT,
    UnknownTransportError,
    resolve_transport,
)

# --------------------------------------------------------------------------- #
# resolve_transport: default seguro + normalización + fail-closed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["", "   "])
def test_resolve_transport_default_is_direct(value: str) -> None:
    assert resolve_transport({"CAMCOUNTER_SYNC_TRANSPORT": value}) == TRANSPORT_DIRECT


def test_resolve_transport_missing_key_is_direct() -> None:
    assert resolve_transport({}) == TRANSPORT_DIRECT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("iot", TRANSPORT_IOT),
        ("IOT", TRANSPORT_IOT),
        ("  iot  ", TRANSPORT_IOT),
        ("Direct", TRANSPORT_DIRECT),
        ("DIRECT", TRANSPORT_DIRECT),
    ],
)
def test_resolve_transport_normalizes(raw: str, expected: str) -> None:
    assert resolve_transport({"CAMCOUNTER_SYNC_TRANSPORT": raw}) == expected


@pytest.mark.parametrize("bad", ["mqtt", "https", "iott", "dir", "1"])
def test_resolve_transport_unknown_raises(bad: str) -> None:
    with pytest.raises(UnknownTransportError):
        resolve_transport({"CAMCOUNTER_SYNC_TRANSPORT": bad})


# --------------------------------------------------------------------------- #
# main: gate de SYNC_ENABLED + enrutado + fail-closed
# --------------------------------------------------------------------------- #


def _spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Monkeypatchea los runners de ambos transportes y registra a cuál se llamó."""
    calls = {"direct": 0, "iot": 0}

    def fake_direct(_argv: object = None) -> int:
        calls["direct"] += 1
        return 0

    def fake_iot(_argv: object = None) -> int:
        calls["iot"] += 1
        return 0

    monkeypatch.setattr("cam_counter_edge.sync_runner.main", fake_direct)
    monkeypatch.setattr("cam_counter_edge.mqtt_publisher.main", fake_iot)
    return calls


def test_main_noop_when_sync_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    monkeypatch.delenv("CAMCOUNTER_SYNC_ENABLED", raising=False)
    monkeypatch.setenv("CAMCOUNTER_SYNC_TRANSPORT", "iot")
    assert sync_dispatch.main() == 0
    assert calls == {"direct": 0, "iot": 0}


def test_main_routes_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    monkeypatch.setenv("CAMCOUNTER_SYNC_ENABLED", "1")
    monkeypatch.setenv("CAMCOUNTER_SYNC_TRANSPORT", "direct")
    assert sync_dispatch.main() == 0
    assert calls == {"direct": 1, "iot": 0}


def test_main_routes_iot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    monkeypatch.setenv("CAMCOUNTER_SYNC_ENABLED", "1")
    monkeypatch.setenv("CAMCOUNTER_SYNC_TRANSPORT", "iot")
    assert sync_dispatch.main() == 0
    assert calls == {"direct": 0, "iot": 1}


def test_main_default_transport_is_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    monkeypatch.setenv("CAMCOUNTER_SYNC_ENABLED", "1")
    monkeypatch.delenv("CAMCOUNTER_SYNC_TRANSPORT", raising=False)
    assert sync_dispatch.main() == 0
    assert calls == {"direct": 1, "iot": 0}


def test_main_propagates_runner_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cam_counter_edge.mqtt_publisher.main", lambda _a=None: 2)
    monkeypatch.setenv("CAMCOUNTER_SYNC_ENABLED", "1")
    monkeypatch.setenv("CAMCOUNTER_SYNC_TRANSPORT", "iot")
    assert sync_dispatch.main() == 2


def test_main_unknown_transport_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    monkeypatch.setenv("CAMCOUNTER_SYNC_ENABLED", "1")
    monkeypatch.setenv("CAMCOUNTER_SYNC_TRANSPORT", "mqtt")  # errata típica
    # Fail-closed: ni arranca direct ni iot; sale != 0.
    assert sync_dispatch.main() == 2
    assert calls == {"direct": 0, "iot": 0}


# --------------------------------------------------------------------------- #
# Fail-closed de material mTLS del modo iot (cert/key/CA al boot)
# --------------------------------------------------------------------------- #


def _full_cert_env() -> dict[str, str]:
    return {
        "CAMCOUNTER_IOT_CERT_PATH": "/etc/cam-counter/certs/device.cert.pem",
        "CAMCOUNTER_IOT_KEY_PATH": "/etc/cam-counter/certs/device.private.key",
        "CAMCOUNTER_IOT_ROOT_CA_PATH": "/etc/cam-counter/certs/AmazonRootCA1.pem",
    }


def test_missing_cert_material_all_present() -> None:
    # exists inyectado a True: todas las vars definidas y "existen" -> sin problemas.
    assert missing_iot_cert_material(_full_cert_env(), exists=lambda _p: True) == []


@pytest.mark.parametrize("drop", sorted(_full_cert_env().keys()))
def test_missing_cert_material_reports_unset_var(drop: str) -> None:
    env = _full_cert_env()
    del env[drop]
    reasons = missing_iot_cert_material(env, exists=lambda _p: True)
    assert any(drop in r for r in reasons)


def test_missing_cert_material_reports_nonexistent_file() -> None:
    reasons = missing_iot_cert_material(_full_cert_env(), exists=lambda _p: False)
    assert len(reasons) == 3
    assert all("no existe" in r for r in reasons)


def test_iot_main_fails_closed_without_cert_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Identidad mínima presente pero SIN material mTLS: el entrypoint iot debe ABORTAR
    # (return 2) antes de tocar Store/red, en vez de arrancar mudo.
    monkeypatch.setenv("CAMCOUNTER_IOT_THING_NAME", "cam-counter-casa-rpi-cam")
    monkeypatch.setenv("CAMCOUNTER_IOT_ENDPOINT", "x.iot.us-east-1.amazonaws.com")
    monkeypatch.setenv("CAMCOUNTER_DEVICE_ID", "rpi-cam")
    for key in _full_cert_env():
        monkeypatch.delenv(key, raising=False)
    assert mqtt_publisher.main() == 2
