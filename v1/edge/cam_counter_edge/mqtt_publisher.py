"""Publicador MQTT del device (modo ``iot``) — paho-mqtt mTLS, drenado ack-driven.

Reescribe el camino de sincronización edge->cloud sobre **AWS IoT Core** SIN cortar el
camino directo (eso es un WP posterior): el publicador convive con el sync directo bajo
el flag ``CAMCOUNTER_SYNC_TRANSPORT`` (``direct`` por defecto; ``iot`` = sólo MQTT). El
dual-run (ambos a la vez, para validar paridad) es seguro porque la idempotencia
(``event_id`` determinista + conditional put ``PK AND SK`` en la Lambda) impide duplicar.

Garantías (criterios de aceptación WP14):

- ``client_id == CAMCOUNTER_IOT_THING_NAME`` (la policy de WP06 ata client-id al Thing).
- ``clean_session=False`` (sesión persistente: el broker retiene la cola QoS1 entre
  reconexiones).
- ``tls_set`` con cert/key/CA del device (**mTLS**); sin secretos en el repo.
- ``will_set`` (LWT): status ``offline`` **retained** publicado por el broker si el
  device cae sin desconectar limpio.
- **Drenado ack-driven**: publica los ``CrossingEvents WHERE synced=0`` con **QoS1** y
  marca ``synced=1`` SÓLO en ``on_publish`` (PUBACK). Si el device cae entre el publish y
  el PUBACK, el evento sigue ``synced=0`` y se reintenta: la cola SQLite es la **fuente de
  verdad** (edge-first), MQTT es best-effort.
- El **topic DERIVA del device_id** (``cam-counter/{device_id}/...``), el mismo canon que
  la variable de la device-policy de WP06; se valida contra el thing name ANTES de
  publicar (si divergen, IoT denegaría el publish en silencio).
- El payload es el ``CrossingEvent`` del contrato **VERBATIM** (ver ``crossing_payload``).
- La subida de clips usa credenciales temporales del **IoT Credential Provider** (role
  alias), NO credenciales AWS estáticas (ver ``iot_credentials``).

paho-mqtt se importa de forma PEREZOSA (no es dependencia base; el extra ``iot`` lo trae).
El cliente MQTT es INYECTABLE para tests (un fake), de modo que la lógica de drenado /
ack / idempotencia / mapeo se ejercita en CI x86 sin broker ni red.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .crossing_payload import PayloadContractError, crossing_event_payload, encode_payload
from .identifiers import validate_device_id
from .store import Store
from .sync import AwsClients, is_precondition_failed, upload_event_clip

__all__ = [
    "MqttPublisher",
    "PublishOutcome",
    "derive_topic_prefix",
    "validate_thing_topic",
    "main",
]

_log = logging.getLogger(__name__)

# Prefijo de topic canónico (naming-standard §3): cam-counter/{device_id}. La
# device-policy de WP06 concede publish sobre
# ``cam-counter/${iot:Connection.Thing.Attributes[device_id]}/*``; el topic DEBE
# derivar del MISMO device_id o el broker deniega el publish en silencio.
PRODUCT_PREFIX = "cam-counter"

# Sufijos de topic (canon compartido con provision-device.sh).
EVENTS_SUFFIX = "events/crossing"
STATUS_SUFFIX = "status"
TELEMETRY_SUFFIX = "telemetry"

# QoS1 (at-least-once): junto con la idempotencia del contrato da exactly-once efectivo.
QOS_AT_LEAST_ONCE = 1

# rc de paho que indica "encolado con éxito" (MQTT_ERR_SUCCESS == 0).
_MQTT_ERR_SUCCESS = 0


# --------------------------------------------------------------------------- #
# Derivación y validación de topics (DERIVAN del thing name / device_id)
# --------------------------------------------------------------------------- #


def derive_topic_prefix(device_id: str) -> str:
    """``cam-counter/{device_id}`` — prefijo de topic canónico (slug validado)."""
    validate_device_id(device_id)
    return f"{PRODUCT_PREFIX}/{device_id}"


def validate_thing_topic(thing_name: str, device_id: str) -> str:
    """Valida que topic y thing name DERIVAN del mismo ``device_id``; devuelve el prefijo.

    Canon (provision-device.sh / naming-standard / policy de WP06):
      - thing name = ``cam-counter-{site_id}-{device_id}`` (client-id == thing).
      - topic prefix = ``cam-counter/{device_id}``.

    Exige que el thing name TERMINE en ``-{device_id}`` y empiece por ``cam-counter-``.
    Si no, ABORTA (fail-closed): publicar con un topic que no case con la variable de la
    policy haría que IoT denegara el publish en SILENCIO. Llamar ANTES de activar dual.
    """
    validate_device_id(device_id)
    if not thing_name:
        raise ValueError("thing name vacío: no se puede derivar/validar el topic")
    suffix = f"-{device_id}"
    if not thing_name.startswith(f"{PRODUCT_PREFIX}-") or not thing_name.endswith(suffix):
        raise ValueError(
            f"thing name {thing_name!r} no deriva del device_id {device_id!r} "
            f"(canon esperado cam-counter-<site>-{device_id}); el topic divergiría de "
            f"la variable de la device-policy y el publish se denegaría en silencio."
        )
    return derive_topic_prefix(device_id)


# --------------------------------------------------------------------------- #
# Interfaz mínima del cliente MQTT (inyectable; paho-compatible)
# --------------------------------------------------------------------------- #


class _MqttMessageInfoLike(Protocol):
    """Forma del ``MQTTMessageInfo`` de paho que usa el publicador."""

    rc: int
    mid: int


class _MqttClientLike(Protocol):
    """Subconjunto de la API de ``paho.mqtt.client.Client`` que usa el publicador."""

    on_connect: Any
    on_disconnect: Any
    on_publish: Any

    def tls_set(self, **kwargs: Any) -> Any: ...
    def will_set(self, topic: str, payload: Any, qos: int, retain: bool) -> Any: ...
    def connect(self, host: str, port: int, keepalive: int) -> Any: ...
    def loop_start(self) -> Any: ...
    def loop_stop(self) -> Any: ...
    def publish(
        self, topic: str, payload: Any, qos: int, retain: bool
    ) -> _MqttMessageInfoLike: ...
    def disconnect(self) -> Any: ...


class PublishOutcome:
    """Resultado de un ``drain_once`` del publicador (observabilidad / tests)."""

    def __init__(self) -> None:
        self.published = 0  # publish() aceptado por el cliente (encolado QoS1)
        self.acked = 0  # PUBACK recibidos en esta pasada (synced=1)
        self.rejected_contract = 0  # payloads que NO casaron el contrato (fail-closed)
        self.clips_uploaded = 0
        self.stopped_offline = False  # el cliente rechazó un publish (red/cola llena)


# --------------------------------------------------------------------------- #
# Publicador MQTT
# --------------------------------------------------------------------------- #


class MqttPublisher:
    """Publica ``CrossingEvents`` por MQTT (QoS1, ack-driven, mTLS).

    Args:
        store: capa SQLite del borde (fuente de verdad; expone ``get_unsynced_events``
            y ``mark_event_synced`` + helpers de clips).
        thing_name: nombre del Thing IoT == ``client_id`` (canon de la policy WP06).
        device_id: slug del device; de él DERIVA el topic.
        endpoint/cert_path/key_path/ca_path/port: parámetros mTLS del broker.
        client: cliente MQTT inyectado (fake en CI); si ``None`` se construye con
            ``client_factory`` perezosamente (paho real).
        clip_clients_provider: callable que devuelve ``AwsClients`` (S3) con
            credenciales temporales del IoT Credential Provider, para subir clips. Si
            ``None``, no se suben clips (el evento se publica sin ``clip_key``).
        media_bucket: bucket de media para los clips.
        app_version: versión reportada en la telemetría/heartbeat.
    """

    def __init__(
        self,
        store: Any,
        *,
        thing_name: str,
        device_id: str,
        endpoint: str,
        cert_path: str = "",
        key_path: str = "",
        ca_path: str = "",
        port: int = 8883,
        keepalive: int = 60,
        client: _MqttClientLike | None = None,
        client_factory: Callable[[str], _MqttClientLike] | None = None,
        clip_clients_provider: Callable[[], AwsClients] | None = None,
        media_bucket: str = "",
        app_version: str = "edge-dev",
    ) -> None:
        self._store = store
        self._thing_name = thing_name
        self._device_id = validate_device_id(device_id)
        self._topic_prefix = validate_thing_topic(thing_name, device_id)
        self._endpoint = endpoint
        self._cert_path = cert_path
        self._key_path = key_path
        self._ca_path = ca_path
        self._port = port
        self._keepalive = keepalive
        self._client = client
        self._client_factory = client_factory
        self._clip_clients_provider = clip_clients_provider
        self._media_bucket = media_bucket
        self._app_version = app_version

        self._events_topic = f"{self._topic_prefix}/{EVENTS_SUFFIX}"
        self._status_topic = f"{self._topic_prefix}/{STATUS_SUFFIX}"
        self._telemetry_topic = f"{self._topic_prefix}/{TELEMETRY_SUFFIX}"

        # mid (PUBACK pendiente) -> event_id. Protegido por lock: on_publish corre en
        # el hilo de red de paho mientras drain_once corre en el hilo principal.
        self._inflight: dict[int, str] = {}
        self._lock = threading.Lock()
        self._acked_since_drain = 0

    # -- topics (lectura para tests/observabilidad) ----------------------

    @property
    def events_topic(self) -> str:
        return self._events_topic

    @property
    def status_topic(self) -> str:
        return self._status_topic

    @property
    def telemetry_topic(self) -> str:
        return self._telemetry_topic

    # -- callbacks paho ---------------------------------------------------

    def _on_connect(self, *_args: Any, **_kwargs: Any) -> None:
        """Al (re)conectar: publica status online retained."""
        self.publish_status(online=True)

    def _on_disconnect(self, *_args: Any, **_kwargs: Any) -> None:
        _log.warning("mqtt-publisher: desconectado del broker; paho reintentará")

    def _on_publish(self, *args: Any, **_kwargs: Any) -> None:
        """PUBACK recibido (QoS1): marca ``synced=1`` SÓLO ahora (ack-driven).

        La firma de paho varía entre v1 (``client, userdata, mid``) y v2
        (``client, userdata, mid, reason_code, properties``); tomamos el ``mid`` de
        forma posicional defensiva.
        """
        mid = args[2] if len(args) >= 3 else None
        if mid is None:
            return
        with self._lock:
            event_id = self._inflight.pop(int(mid), None)
        if event_id is None:
            return  # PUBACK de un publish no-evento (status/telemetría) o ya procesado
        if self._store.mark_event_synced(event_id):
            with self._lock:
                self._acked_since_drain += 1
            _log.debug("mqtt-publisher: PUBACK %s -> synced=1", event_id)

    # -- cliente MQTT -----------------------------------------------------

    def _ensure_client(self) -> _MqttClientLike:
        if self._client is None:
            if self._client_factory is None:
                raise RuntimeError(
                    "MqttPublisher sin cliente ni client_factory: inyecta un cliente "
                    "(fake en tests) o usa default_mqtt_client_factory (paho real)."
                )
            self._client = self._client_factory(self._thing_name)
        return self._client

    def connect(self) -> None:
        """Configura TLS/LWT/callbacks y conecta al broker (loop en hilo de red)."""
        client = self._ensure_client()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_publish = self._on_publish
        if self._ca_path or self._cert_path or self._key_path:
            client.tls_set(
                ca_certs=self._ca_path or None,
                certfile=self._cert_path or None,
                keyfile=self._key_path or None,
            )
        # LWT: status offline retained si el device cae sin desconectar limpio.
        client.will_set(
            self._status_topic,
            encode_payload(self._status_payload(online=False)),
            qos=QOS_AT_LEAST_ONCE,
            retain=True,
        )
        client.connect(self._endpoint, self._port, self._keepalive)
        client.loop_start()

    # -- status / telemetría ---------------------------------------------

    def _status_payload(self, *, online: bool) -> dict[str, Any]:
        return {
            "device_id": self._device_id,
            "thing_name": self._thing_name,
            "status": "online" if online else "offline",
            "agent_version": self._app_version,
            "ts_ms": int(time.time() * 1000),
        }

    def publish_status(self, *, online: bool) -> None:
        """Publica el status online/offline (retained) en el topic de status."""
        client = self._ensure_client()
        client.publish(
            self._status_topic,
            encode_payload(self._status_payload(online=online)),
            qos=QOS_AT_LEAST_ONCE,
            retain=True,
        )

    def publish_telemetry(self, extra: dict[str, Any] | None = None) -> None:
        """Publica un heartbeat/telemetría best-effort (no retained)."""
        client = self._ensure_client()
        payload: dict[str, Any] = {
            "device_id": self._device_id,
            "thing_name": self._thing_name,
            "agent_version": self._app_version,
            "ts_ms": int(time.time() * 1000),
            "pending": self._safe_pending_count(),
        }
        if extra:
            payload.update(extra)
        client.publish(
            self._telemetry_topic,
            encode_payload(payload),
            qos=QOS_AT_LEAST_ONCE,
            retain=False,
        )

    def _safe_pending_count(self) -> int:
        try:
            return int(self._store.count_unsynced_events())
        except Exception:  # noqa: BLE001 — telemetría best-effort
            return -1

    # -- subida de clip (vía IoT Credential Provider) --------------------

    def _maybe_upload_clip(self, event: Any) -> bool:
        """Sube el clip del evento (si procede) y fija ``clip_key`` antes de publicar.

        Best-effort: si no hay provider de credenciales o falla la subida, el evento se
        publica igual SIN ``clip_key`` (edge-first: nunca bloquea el drenado del evento).
        Devuelve ``True`` si subió un objeto nuevo.
        """
        if self._clip_clients_provider is None or not self._media_bucket:
            return False
        clip_row = self._store.get_clip_upload_for_event(event.event_id)
        if clip_row is None:
            return False
        try:
            clients = self._clip_clients_provider()
            key, uploaded, already = upload_event_clip(
                clients.s3, self._store, clip_row, self._media_bucket
            )
        except Exception as exc:  # noqa: BLE001
            if is_precondition_failed(exc):
                return False
            _log.warning(
                "mqtt-publisher: subida de clip de %s falló (%r); publico sin clip_key",
                event.event_id,
                exc,
            )
            return False
        if key is not None:
            self._store.set_event_clip_key(event.event_id, key, "uploaded")
            event.clip_key = key
            event.clip_status = "uploaded"
            return uploaded and not already
        return False

    # -- drenado ----------------------------------------------------------

    def drain_once(self, *, limit: int = 100) -> PublishOutcome:
        """Publica un batch de eventos ``synced=0`` (QoS1). NO marca synced (lo hace PUBACK).

        Por cada evento: sube su clip (best-effort) -> mapea a payload verbatim
        (fail-closed) -> publica QoS1 y registra ``mid -> event_id`` para que el PUBACK
        lo marque ``synced=1``. Si el cliente rechaza un publish (rc != success: red
        caída / cola llena), DETIENE el batch (edge-first: el evento sigue synced=0).
        """
        outcome = PublishOutcome()
        with self._lock:
            self._acked_since_drain = 0
        client = self._ensure_client()
        events = self._store.get_unsynced_events(limit)
        for event in events:
            if self._maybe_upload_clip(event):
                outcome.clips_uploaded += 1
            try:
                payload = crossing_event_payload(event)
            except PayloadContractError as exc:
                outcome.rejected_contract += 1
                _log.error(
                    "mqtt-publisher: evento %s NO casa el contrato (%s); NO se publica",
                    event.event_id,
                    "; ".join(exc.reasons),
                )
                continue
            info = client.publish(
                self._events_topic,
                encode_payload(payload),
                qos=QOS_AT_LEAST_ONCE,
                retain=False,
            )
            if getattr(info, "rc", _MQTT_ERR_SUCCESS) != _MQTT_ERR_SUCCESS:
                # El cliente no pudo encolar (offline / cola llena): para el batch.
                outcome.stopped_offline = True
                _log.warning(
                    "mqtt-publisher: publish rechazado (rc=%s) en %s; reintento luego",
                    getattr(info, "rc", "?"),
                    event.event_id,
                )
                break
            with self._lock:
                self._inflight[int(info.mid)] = event.event_id
            outcome.published += 1
        with self._lock:
            outcome.acked = self._acked_since_drain
        return outcome

    def close(self) -> None:
        """Publica status offline (retained), para el loop y desconecta limpio."""
        if self._client is None:
            return
        try:
            self.publish_status(online=False)
        except Exception as exc:  # noqa: BLE001
            _log.debug("mqtt-publisher: no se pudo publicar offline al cerrar (%r)", exc)
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            _log.debug("mqtt-publisher: error al desconectar (%r)", exc)


# --------------------------------------------------------------------------- #
# Factories por defecto (paho real / credenciales reales) — imports PEREZOSOS
# --------------------------------------------------------------------------- #


def default_mqtt_client_factory(client_id: str) -> _MqttClientLike:
    """Construye un ``paho.mqtt.client.Client`` real (import PEREZOSO).

    ``clean_session=False`` (sesión persistente para la cola QoS1). Compatible con
    paho v1 y v2 (en v2 hace falta ``CallbackAPIVersion``).
    """
    import paho.mqtt.client as mqtt  # noqa: PLC0415

    callback_api = getattr(mqtt, "CallbackAPIVersion", None)
    if callback_api is not None:  # paho-mqtt >= 2.0
        return mqtt.Client(
            callback_api_version=callback_api.VERSION1,
            client_id=client_id,
            clean_session=False,
        )
    return mqtt.Client(client_id=client_id, clean_session=False)  # paho-mqtt 1.x


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _build_clip_provider() -> Callable[[], AwsClients] | None:
    """Construye el provider de credenciales del IoT Credential Provider, si hay config.

    Devuelve ``None`` si falta la config de credential provider (los clips no se suben;
    el evento se publica igual sin ``clip_key``). Import PEREZOSO de ``iot_credentials``.
    """
    cred_endpoint = _env("CAMCOUNTER_IOT_CRED_ENDPOINT")
    role_alias = _env("CAMCOUNTER_ROLE_ALIAS")
    cert = _env("CAMCOUNTER_IOT_CERT_PATH")
    key = _env("CAMCOUNTER_IOT_KEY_PATH")
    ca = _env("CAMCOUNTER_IOT_ROOT_CA_PATH")
    region = _env("CAMCOUNTER_AWS_REGION", "us-east-1")
    if not (cred_endpoint and role_alias and cert and key and ca):
        _log.info(
            "mqtt-publisher: sin config completa de IoT Credential Provider; "
            "los clips no se subirán (eventos sin clip_key)."
        )
        return None

    from .iot_credentials import IotCredentialProvider  # noqa: PLC0415

    provider = IotCredentialProvider(
        endpoint=cred_endpoint,
        role_alias=role_alias,
        cert_path=cert,
        key_path=key,
        ca_path=ca,
        region=region,
    )
    return provider.clients


def main(argv: list[str] | None = None) -> int:
    """Entrypoint del publicador MQTT (modo ``iot``). Devuelve 0 al parar.

    Lee la identidad IoT del entorno (``CAMCOUNTER_IOT_*`` que escribe
    provision-device.sh). Best-effort y edge-first: nunca muere por un fallo de red.
    """
    logging.basicConfig(level=logging.INFO)

    db_path = _env("CAMCOUNTER_DB_PATH", "cam-counter.db")
    device_id = _env("CAMCOUNTER_DEVICE_ID", "demo-pi")
    thing_name = _env("CAMCOUNTER_IOT_THING_NAME") or _env("CAMCOUNTER_IOT_CLIENT_ID")
    endpoint = _env("CAMCOUNTER_IOT_ENDPOINT")
    if not thing_name or not endpoint:
        _log.error(
            "mqtt-publisher: faltan CAMCOUNTER_IOT_THING_NAME/CAMCOUNTER_IOT_ENDPOINT; "
            "provisiona el device (scripts/provision-device.sh) antes de modo iot."
        )
        return 2

    # WP16 — fail-closed de la identidad mTLS al boot. IoT Core EXIGE mTLS: sin
    # cert/key/CA el publish nunca conectaría y los eventos se acumularían en silencio.
    # Abortamos explícitamente (en vez de degradar a "sin clip_key") para que un corte
    # a modo iot con identidad incompleta sea ruidoso y NO un fallo mudo.
    cert_path = _env("CAMCOUNTER_IOT_CERT_PATH")
    key_path = _env("CAMCOUNTER_IOT_KEY_PATH")
    ca_path = _env("CAMCOUNTER_IOT_ROOT_CA_PATH")
    missing = [
        name
        for name, value in (
            ("CAMCOUNTER_IOT_CERT_PATH", cert_path),
            ("CAMCOUNTER_IOT_KEY_PATH", key_path),
            ("CAMCOUNTER_IOT_ROOT_CA_PATH", ca_path),
        )
        if not value or not os.path.isfile(value)
    ]
    if missing:
        _log.error(
            "mqtt-publisher: identidad mTLS incompleta (faltan o no existen: %s); IoT "
            "Core exige cert/key/CA. Provisiona el device (scripts/provision-device.sh) "
            "antes de modo iot.",
            ", ".join(missing),
        )
        return 2

    try:
        interval_s = max(2.0, float(_env("CAMCOUNTER_SYNC_INTERVAL_S", "10")))
    except ValueError:
        interval_s = 10.0

    store = Store(db_path)
    try:
        publisher = MqttPublisher(
            store,
            thing_name=thing_name,
            device_id=device_id,
            endpoint=endpoint,
            cert_path=cert_path,
            key_path=key_path,
            ca_path=ca_path,
            client_factory=default_mqtt_client_factory,
            clip_clients_provider=_build_clip_provider(),
            media_bucket=_env("CAMCOUNTER_MEDIA_BUCKET", "cam-counter-media-950639281773"),
            app_version=_env("CAMCOUNTER_APP_VERSION", "edge-dev"),
        )
    except ValueError as exc:
        # validate_thing_topic falló: topic divergiría de la policy (fail-closed).
        _log.error("mqtt-publisher: configuración de identidad inválida: %s", exc)
        store.close()
        return 2

    stop = threading.Event()

    def _handle(_signum: int, _frame: Any) -> None:
        _log.info("mqtt-publisher: señal recibida; parando…")
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    _log.info(
        "mqtt-publisher: thing=%s device=%s endpoint=%s topic=%s intervalo=%ss",
        thing_name,
        device_id,
        endpoint,
        publisher.events_topic,
        interval_s,
    )

    try:
        publisher.connect()
    except Exception as exc:  # noqa: BLE001 — edge-first: no morir por fallo de conexión
        _log.warning("mqtt-publisher: conexión inicial falló (%r); paho reintentará", exc)

    last_telemetry = 0.0
    while not stop.is_set():
        try:
            outcome = publisher.drain_once()
            if outcome.published or outcome.acked or outcome.rejected_contract:
                _log.info(
                    "mqtt-publisher: publicados=%d ack=%d rechazados=%d clips=%d offline=%s",
                    outcome.published,
                    outcome.acked,
                    outcome.rejected_contract,
                    outcome.clips_uploaded,
                    outcome.stopped_offline,
                )
        except Exception as exc:  # noqa: BLE001 — el publicador NUNCA debe morir
            _log.warning("mqtt-publisher: error en el drenado (%r); reintento luego", exc)

        now = time.monotonic()
        if now - last_telemetry > 60.0:
            try:
                publisher.publish_telemetry()
                last_telemetry = now
            except Exception as exc:  # noqa: BLE001
                _log.debug("mqtt-publisher: telemetría falló (%r)", exc)

        stop.wait(interval_s)

    publisher.close()
    store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
