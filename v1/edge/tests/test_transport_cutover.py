"""Tests del corte del camino directo del proceso edge (WP16).

Cubren el selector de transporte (``CAMCOUNTER_SYNC_TRANSPORT``) que decide si el
proceso de borde sincroniza por el camino DIRECTO (boto3 -> DynamoDB/S3, credenciales
AWS) o por MQTT (IoT Core + role alias, SIN credenciales AWS directas), y el
fail-closed de identidad mTLS del publicador al boot. Todo en x86 sin AWS ni broker.
"""

from __future__ import annotations

import threading

import pytest

from cam_counter_edge import mqtt_publisher, sync_runner

# --------------------------------------------------------------------------- #
# resolve_transport: 'iot' explícito vs. todo lo demás -> 'direct'
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, sync_runner.TRANSPORT_DIRECT),  # sin definir -> direct
        ("", sync_runner.TRANSPORT_DIRECT),
        ("direct", sync_runner.TRANSPORT_DIRECT),
        ("iot", sync_runner.TRANSPORT_IOT),
        ("IOT", sync_runner.TRANSPORT_IOT),  # case-insensitive
        ("  iot  ", sync_runner.TRANSPORT_IOT),  # con espacios
        ("itot", sync_runner.TRANSPORT_DIRECT),  # typo -> direct (no caer en iot)
        ("mqtt", sync_runner.TRANSPORT_DIRECT),
    ],
)
def test_resolve_transport(monkeypatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv("CAMCOUNTER_SYNC_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("CAMCOUNTER_SYNC_TRANSPORT", value)
    assert sync_runner.resolve_transport() == expected


# --------------------------------------------------------------------------- #
# El camino directo queda INERTE en modo iot: sale sin tocar boto3/STES/SQLite
# --------------------------------------------------------------------------- #


def test_direct_path_inert_in_iot_mode(monkeypatch) -> None:
    """En modo iot, sync_runner.main() sale 0 SIN construir clientes AWS ni Store.

    Es la garantía del corte: el proceso de borde deja de usar credenciales AWS
    directas. Si tocara boto3 (default_client_factory) o abriera el Store, estos
    stubs lanzarían y el test fallaría.
    """

    def _boom_factory(*_a, **_k):  # pragma: no cover - debe NO llamarse
        raise AssertionError("modo iot no debe construir clientes AWS directos")

    def _boom_store(*_a, **_k):  # pragma: no cover - debe NO llamarse
        raise AssertionError("modo iot no debe abrir el Store del camino directo")

    monkeypatch.setattr(sync_runner, "default_client_factory", _boom_factory)
    monkeypatch.setattr(sync_runner, "Store", _boom_store)
    monkeypatch.setenv("CAMCOUNTER_SYNC_ENABLED", "1")
    monkeypatch.setenv("CAMCOUNTER_SYNC_TRANSPORT", "iot")

    assert sync_runner.main([]) == 0


def test_disabled_short_circuits_before_transport(monkeypatch) -> None:
    """Sin CAMCOUNTER_SYNC_ENABLED, main() sale 0 sea cual sea el transporte."""
    monkeypatch.delenv("CAMCOUNTER_SYNC_ENABLED", raising=False)
    monkeypatch.setenv("CAMCOUNTER_SYNC_TRANSPORT", "iot")
    assert sync_runner.main([]) == 0


# --------------------------------------------------------------------------- #
# mqtt_publisher: fail-closed de identidad al boot (thing/endpoint y mTLS)
# --------------------------------------------------------------------------- #


def _clear_iot_env(monkeypatch) -> None:
    for key in (
        "CAMCOUNTER_IOT_THING_NAME",
        "CAMCOUNTER_IOT_CLIENT_ID",
        "CAMCOUNTER_IOT_ENDPOINT",
        "CAMCOUNTER_IOT_CERT_PATH",
        "CAMCOUNTER_IOT_KEY_PATH",
        "CAMCOUNTER_IOT_ROOT_CA_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


def test_mqtt_fail_closed_without_thing_or_endpoint(monkeypatch) -> None:
    _clear_iot_env(monkeypatch)
    assert mqtt_publisher.main([]) == 2


def test_mqtt_fail_closed_without_certs(monkeypatch) -> None:
    """Con thing+endpoint pero SIN cert/key/CA, aborta (IoT Core exige mTLS)."""
    _clear_iot_env(monkeypatch)
    monkeypatch.setenv("CAMCOUNTER_IOT_THING_NAME", "cam-counter-casa-rpi-001")
    monkeypatch.setenv("CAMCOUNTER_IOT_ENDPOINT", "xyz-ats.iot.us-east-1.amazonaws.com")
    assert mqtt_publisher.main([]) == 2


def test_mqtt_fail_closed_with_missing_cert_files(monkeypatch, tmp_path) -> None:
    """Rutas de cert definidas pero inexistentes -> fail-closed (no fallo mudo)."""
    _clear_iot_env(monkeypatch)
    monkeypatch.setenv("CAMCOUNTER_IOT_THING_NAME", "cam-counter-casa-rpi-001")
    monkeypatch.setenv("CAMCOUNTER_IOT_ENDPOINT", "xyz-ats.iot.us-east-1.amazonaws.com")
    monkeypatch.setenv("CAMCOUNTER_IOT_CERT_PATH", str(tmp_path / "nope.cert.pem"))
    monkeypatch.setenv("CAMCOUNTER_IOT_KEY_PATH", str(tmp_path / "nope.key"))
    monkeypatch.setenv("CAMCOUNTER_IOT_ROOT_CA_PATH", str(tmp_path / "nope.ca"))
    assert mqtt_publisher.main([]) == 2


def test_mqtt_boots_past_identity_with_valid_certs(monkeypatch, tmp_path) -> None:
    """Con identidad mTLS completa, el boot pasa el gate y construye el publicador.

    Inyectamos un cliente MQTT fake (sin red) y paramos el bucle de inmediato vía
    señal para no bloquear: lo que validamos es que NO aborta por identidad.
    """
    cert = tmp_path / "device.cert.pem"
    key = tmp_path / "device.key"
    ca = tmp_path / "AmazonRootCA1.pem"
    for path in (cert, key, ca):
        path.write_text("x")

    _clear_iot_env(monkeypatch)
    monkeypatch.setenv("CAMCOUNTER_IOT_THING_NAME", "cam-counter-casa-rpi-001")
    monkeypatch.setenv("CAMCOUNTER_DEVICE_ID", "rpi-001")
    monkeypatch.setenv("CAMCOUNTER_IOT_ENDPOINT", "xyz-ats.iot.us-east-1.amazonaws.com")
    monkeypatch.setenv("CAMCOUNTER_IOT_CERT_PATH", str(cert))
    monkeypatch.setenv("CAMCOUNTER_IOT_KEY_PATH", str(key))
    monkeypatch.setenv("CAMCOUNTER_IOT_ROOT_CA_PATH", str(ca))
    monkeypatch.setenv("CAMCOUNTER_DB_PATH", str(tmp_path / "edge.db"))

    class _FakeInfo:
        rc = 0
        mid = 1

    class _FakeClient:
        def __init__(self) -> None:
            self.on_connect = self.on_disconnect = self.on_publish = None

        def tls_set(self, **_k):  # noqa: D401
            return None

        def will_set(self, *_a, **_k):
            return None

        def connect(self, *_a, **_k):
            return None

        def loop_start(self):
            return None

        def loop_stop(self):
            return None

        def publish(self, *_a, **_k):
            return _FakeInfo()

        def disconnect(self):
            return None

    monkeypatch.setattr(
        mqtt_publisher, "default_mqtt_client_factory", lambda _cid: _FakeClient()
    )

    # Registramos que el publicador SÍ se construye (el gate de identidad pasó).
    real_init = mqtt_publisher.MqttPublisher.__init__
    captured: dict = {}

    def _init(self, *a, **k):
        real_init(self, *a, **k)
        captured["publisher"] = self

    monkeypatch.setattr(mqtt_publisher.MqttPublisher, "__init__", _init)

    # Paramos el bucle al primer tick (sin red, sin bloquear). monkeypatch restaura
    # threading.Event.wait al teardown.
    def _wait_once(self, *_a, **_k):
        self.set()
        return True

    monkeypatch.setattr(threading.Event, "wait", _wait_once)

    assert mqtt_publisher.main([]) == 0
    assert "publisher" in captured  # pasó el gate de identidad y construyó el publicador
