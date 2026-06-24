"""Reconciliador de **Device Shadow** nube<->SQLite (config de línea + comandos).

El canal comando/config nube->dispositivo se hace vía **Device Shadow**. Este
módulo es el otro extremo del que escribe la consola de flota: reconcilia el
``desired`` del shadow con el SQLite local, que es la **ÚNICA fuente de verdad** y
el **único punto de aplicación** (sin split-brain). El árbitro es ``config_version``
(**gana la versión mayor**, monótono): la nube debe proponer una versión MAYOR para
ganar; si no, el device la ignora y re-reporta su versión vigente.

Diseño (criterios de aceptación WP15):

- **Por-cámara**: ``line_config`` es POR-CÁMARA (``camera_id`` requerido) pero el
  shadow es por-THING, así que se usa un **named shadow por cámara**
  ``line-config-{camera_id}``. Cada cámara del device tiene el suyo; no se asume
  "1 línea por device".
- **Boot**: al arrancar publica ``.../get`` de cada shadow; el ``get/accepted``
  trae el ``desired`` que llegó estando **offline** y se sincroniza.
- **Delta**: ``.../update/delta`` trae el ``desired`` (parcial) cuando la nube
  cambia algo; se **valida VERBATIM** contra ``contracts/line_config.schema.json``
  (fail-closed); si ``desired.config_version > config_version`` se escribe la línea
  en SQLite con esa versión VERBATIM (convergencia), si no se ignora; en ambos
  casos se publica ``reported``.
- **ConfigWatcher**: la aplicación a SQLite + bump de ``config_version`` hace que
  el ``ConfigWatcher`` existente (que ya sondea ``config_version`` UNA VEZ POR
  FRAME) recargue la geometría EN CALIENTE sin reiniciar el detector.
- **UI local**: cuando la UI local edita el SQLite (bumpea ``config_version``), el
  ``tick()`` periódico detecta el cambio y publica ``reported`` (la UI sigue
  funcionando exactamente como hoy; sólo se observa).
- **Comandos**: ``cmd/request`` -> acción -> ``cmd/ack`` por ``command_id``
  (idempotente, ver ``CommandHandler``); persistentes vía named shadow ``command``.

paho-mqtt es INYECTABLE (igual que ``mqtt_publisher``): el cliente se inyecta como
fake en CI, de modo que toda la reconciliación / validación / arbitraje / comandos
se ejercita en x86 sin broker ni red. ``main()`` reusa la **misma identidad mTLS**
del device (cert/key/CA) que el publicador (WP14).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from collections.abc import Callable
from typing import Any, Protocol

from .command_handler import CommandHandler
from .identifiers import validate_camera_id, validate_device_id
from .line_config_contract import (
    LineConfigContractError,
    line_config_from_document,
    line_config_to_document,
    load_line_config_schema,
)
from .store import Store

__all__ = [
    "ReconcileOutcome",
    "ShadowReconciler",
    "command_request_topic",
    "command_shadow_name",
    "line_config_shadow_name",
    "named_shadow_topic",
    "main",
]

_log = logging.getLogger(__name__)

PRODUCT_PREFIX = "cam-counter"
QOS_AT_LEAST_ONCE = 1

# Named shadow de comandos persistentes.
COMMAND_SHADOW = "command"

# Prefijo de los named shadows de config de línea (uno por cámara).
LINE_CONFIG_SHADOW_PREFIX = "line-config-"


# --------------------------------------------------------------------------- #
# Helpers de topics (named shadows + canal fire-and-forget de comandos)
# --------------------------------------------------------------------------- #


def line_config_shadow_name(camera_id: str) -> str:
    """Named shadow de la config de línea de una cámara: ``line-config-{camera_id}``."""
    validate_camera_id(camera_id)
    return f"{LINE_CONFIG_SHADOW_PREFIX}{camera_id}"


def command_shadow_name() -> str:
    """Named shadow de comandos persistentes: ``command``."""
    return COMMAND_SHADOW


def named_shadow_topic(thing_name: str, shadow_name: str, op: str) -> str:
    """Topic de un named shadow: ``$aws/things/{thing}/shadow/name/{shadow}/{op}``."""
    if not thing_name:
        raise ValueError("thing name vacío: no se puede construir el topic del shadow")
    return f"$aws/things/{thing_name}/shadow/name/{shadow_name}/{op}"


def command_request_topic(device_id: str) -> str:
    """Topic fire-and-forget de petición de comando: ``cam-counter/{device_id}/cmd/request``."""
    validate_device_id(device_id)
    return f"{PRODUCT_PREFIX}/{device_id}/cmd/request"


def command_ack_topic(device_id: str) -> str:
    """Topic fire-and-forget de ack de comando: ``cam-counter/{device_id}/cmd/ack``."""
    validate_device_id(device_id)
    return f"{PRODUCT_PREFIX}/{device_id}/cmd/ack"


def _parse_named_shadow(topic: str) -> tuple[str, str] | None:
    """De un topic ``$aws/things/.../shadow/name/{shadow}/{op}`` saca ``(shadow, op)``.

    Devuelve ``None`` si el topic no es de un named shadow.
    """
    marker = "/shadow/name/"
    idx = topic.find(marker)
    if idx < 0:
        return None
    rest = topic[idx + len(marker):]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        return None
    shadow_name, op = parts[0], parts[1]
    if not shadow_name or not op:
        return None
    return shadow_name, op


# --------------------------------------------------------------------------- #
# Interfaz mínima del cliente MQTT (inyectable; paho-compatible)
# --------------------------------------------------------------------------- #


class _MqttClientLike(Protocol):
    """Subconjunto de ``paho.mqtt.client.Client`` que usa el reconciliador."""

    on_connect: Any
    on_message: Any
    on_disconnect: Any

    def tls_set(self, **kwargs: Any) -> Any: ...
    def connect(self, host: str, port: int, keepalive: int) -> Any: ...
    def loop_start(self) -> Any: ...
    def loop_stop(self) -> Any: ...
    def subscribe(self, topic: str, qos: int) -> Any: ...
    def publish(self, topic: str, payload: Any, qos: int, retain: bool) -> Any: ...
    def disconnect(self) -> Any: ...


class ReconcileOutcome:
    """Resultado de procesar un ``desired`` de config (observabilidad / tests)."""

    def __init__(self) -> None:
        self.applied = False  # se escribió en SQLite (versión mayor ganó)
        self.ignored_stale = False  # desired.config_version <= actual (se ignoró)
        self.rejected_contract = False  # el desired NO casó el contrato (fail-closed)
        self.rejected_camera = False  # el camera_id del doc no casa el del shadow
        self.reported = False  # se publicó reported
        self.new_version: int | None = None  # versión aplicada (si applied)


# --------------------------------------------------------------------------- #
# Reconciliador
# --------------------------------------------------------------------------- #


class ShadowReconciler:
    """Reconcilia los named shadows del device con el SQLite local (fuente de verdad).

    Args:
        store: capa SQLite del borde (único punto de aplicación).
        thing_name: nombre del Thing IoT (raíz de los topics de shadow).
        device_id: slug del device (raíz del canal fire-and-forget de comandos).
        camera_ids: cámaras a reconciliar (una named shadow ``line-config-{id}``
            por cámara). Si está vacío, se descubren de ``store.list_config_cameras``.
        client: cliente MQTT inyectado (fake en CI); si ``None`` se construye con
            ``client_factory`` perezosamente (paho real).
        command_handler: handler de comandos (idempotente). Si ``None``, los
            comandos se rechazan como no soportados.
        schema: schema del contrato ``line_config`` (cargado del repo si ``None``).
        app_version: versión reportada en metadatos best-effort.
    """

    def __init__(
        self,
        store: Any,
        *,
        thing_name: str,
        device_id: str,
        camera_ids: list[str] | None = None,
        client: _MqttClientLike | None = None,
        client_factory: Callable[[str], _MqttClientLike] | None = None,
        command_handler: CommandHandler | None = None,
        schema: dict[str, Any] | None = None,
        endpoint: str = "",
        cert_path: str = "",
        key_path: str = "",
        ca_path: str = "",
        port: int = 8883,
        keepalive: int = 60,
        app_version: str = "edge-dev",
    ) -> None:
        self._store = store
        self._thing_name = thing_name
        self._device_id = validate_device_id(device_id)
        self._client = client
        self._client_factory = client_factory
        self._command_handler = command_handler
        self._schema = schema if schema is not None else load_line_config_schema()
        self._endpoint = endpoint
        self._cert_path = cert_path
        self._key_path = key_path
        self._ca_path = ca_path
        self._port = port
        self._keepalive = keepalive
        self._app_version = app_version

        cams = list(camera_ids) if camera_ids else self._discover_cameras()
        # shadow_name -> camera_id (resuelve N cámaras del device).
        self._shadow_to_camera: dict[str, str] = {}
        self._camera_ids: list[str] = []
        for cam in cams:
            validate_camera_id(cam)
            self._shadow_to_camera[line_config_shadow_name(cam)] = cam
            self._camera_ids.append(cam)

        self._cmd_request_topic = command_request_topic(device_id)
        self._cmd_ack_topic = command_ack_topic(device_id)

        # Última versión reportada por cámara (para detectar ediciones de la UI local).
        self._reported_version: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- introspección (tests / observabilidad) --------------------------

    @property
    def camera_ids(self) -> list[str]:
        return list(self._camera_ids)

    def _discover_cameras(self) -> list[str]:
        try:
            return list(self._store.list_config_cameras())
        except Exception as exc:  # noqa: BLE001 — descubrimiento best-effort
            _log.warning("shadow-reconciler: no se pudieron descubrir cámaras (%r)", exc)
            return []

    def subscriptions(self) -> list[str]:
        """Topics a los que se suscribe (delta + get/accepted de cada shadow + cmd)."""
        topics: list[str] = []
        for shadow in list(self._shadow_to_camera) + [COMMAND_SHADOW]:
            topics.append(named_shadow_topic(self._thing_name, shadow, "update/delta"))
            topics.append(named_shadow_topic(self._thing_name, shadow, "get/accepted"))
        topics.append(self._cmd_request_topic)
        return topics

    # -- cliente MQTT -----------------------------------------------------

    def _ensure_client(self) -> _MqttClientLike:
        if self._client is None:
            if self._client_factory is None:
                raise RuntimeError(
                    "ShadowReconciler sin cliente ni client_factory: inyecta un cliente "
                    "(fake en tests) o usa default_mqtt_client_factory (paho real)."
                )
            self._client = self._client_factory(self._thing_name)
        return self._client

    def connect(self) -> None:
        """Configura TLS/callbacks, conecta y arranca el loop de red (paho)."""
        client = self._ensure_client()
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        if self._ca_path or self._cert_path or self._key_path:
            client.tls_set(
                ca_certs=self._ca_path or None,
                certfile=self._cert_path or None,
                keyfile=self._key_path or None,
            )
        client.connect(self._endpoint, self._port, self._keepalive)
        client.loop_start()

    # -- callbacks paho ---------------------------------------------------

    def _on_connect(self, *_args: Any, **_kwargs: Any) -> None:
        """Al (re)conectar: (re)suscribe y pide el ``get`` de cada shadow (sync de boot)."""
        self.subscribe_all()
        self.request_get_all()

    def _on_disconnect(self, *_args: Any, **_kwargs: Any) -> None:
        _log.warning("shadow-reconciler: desconectado del broker; paho reintentará")

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Adaptador paho -> ``dispatch`` (toma topic + payload de forma defensiva)."""
        topic = getattr(message, "topic", "")
        payload = getattr(message, "payload", b"")
        try:
            self.dispatch(topic, payload)
        except Exception as exc:  # noqa: BLE001 — el reconciliador nunca debe morir
            _log.warning("shadow-reconciler: error procesando %s (%r)", topic, exc)

    # -- suscripción / get de boot ---------------------------------------

    def subscribe_all(self) -> None:
        """Se suscribe a todos los topics (idempotente: re-suscribir no daña)."""
        client = self._ensure_client()
        for topic in self.subscriptions():
            client.subscribe(topic, QOS_AT_LEAST_ONCE)

    def request_get_all(self) -> None:
        """Publica un ``get`` vacío de cada named shadow (sincroniza desired offline)."""
        client = self._ensure_client()
        for shadow in list(self._shadow_to_camera) + [COMMAND_SHADOW]:
            topic = named_shadow_topic(self._thing_name, shadow, "get")
            client.publish(topic, b"", qos=QOS_AT_LEAST_ONCE, retain=False)

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, topic: str, payload: Any) -> str:
        """Enruta un mensaje entrante por topic. Devuelve una etiqueta (tests/observabilidad).

        Etiquetas: ``"line-config:delta"``, ``"line-config:get"``,
        ``"command:delta"``, ``"command:get"``, ``"command:request"``,
        ``"ignored"``.
        """
        if topic == self._cmd_request_topic:
            self._handle_command_request(self._load_json(payload))
            return "command:request"

        parsed = _parse_named_shadow(topic)
        if parsed is None:
            return "ignored"
        shadow_name, op = parsed

        if shadow_name == COMMAND_SHADOW:
            if op == "update/delta":
                self._handle_command_shadow(self._delta_state(self._load_json(payload)))
                return "command:delta"
            if op == "get/accepted":
                self._handle_command_get_accepted(self._load_json(payload))
                return "command:get"
            return "ignored"

        camera_id = self._shadow_to_camera.get(shadow_name)
        if camera_id is None:
            return "ignored"  # shadow de una cámara que no gestionamos

        if op == "update/delta":
            self.reconcile_desired(camera_id, self._delta_state(self._load_json(payload)))
            return "line-config:delta"
        if op == "get/accepted":
            self._handle_line_config_get_accepted(camera_id, self._load_json(payload))
            return "line-config:get"
        if op in ("get/rejected", "update/rejected"):
            _log.info("shadow-reconciler: %s %s rechazado por el broker", shadow_name, op)
        return "ignored"

    @staticmethod
    def _load_json(payload: Any) -> dict[str, Any]:
        """Decodifica el payload a dict (fail-closed: ``{}`` si no es JSON-objeto)."""
        if isinstance(payload, dict):
            return payload
        if payload is None or payload == b"" or payload == "":
            return {}
        if isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload).decode("utf-8", errors="replace")
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _delta_state(doc: dict[str, Any]) -> dict[str, Any]:
        """``state`` de un mensaje ``update/delta`` (el ``desired`` parcial)."""
        state = doc.get("state")
        return state if isinstance(state, dict) else {}

    # -- reconciliación de config de línea --------------------------------

    def _handle_line_config_get_accepted(
        self, camera_id: str, doc: dict[str, Any]
    ) -> None:
        """``get/accepted``: reconcilia el ``desired`` del documento completo del shadow.

        Si no hay ``desired`` (shadow vacío), simplemente publica ``reported`` con
        la config local vigente (sincroniza al boot que el device ya tiene config).
        """
        state = doc.get("state")
        desired = state.get("desired") if isinstance(state, dict) else None
        if isinstance(desired, dict) and desired:
            self.reconcile_desired(camera_id, desired)
        else:
            self.publish_reported(camera_id)

    def reconcile_desired(
        self, camera_id: str, desired: dict[str, Any]
    ) -> ReconcileOutcome:
        """Valida VERBATIM, arbitra por ``config_version`` y aplica a SQLite + reporta.

        Pasos: (1) validar el ``desired`` VERBATIM contra el contrato (fail-closed);
        (2) exigir que ``desired.camera_id`` case con la cámara del shadow (anti
        cross-camera); (3) ``apply_remote_line_config`` (gana la versión mayor);
        (4) publicar ``reported`` SIEMPRE (aplicado o ignorado) para que el delta
        del shadow converja. El ``ConfigWatcher`` recarga sólo (sondea SQLite por
        frame): este método NO toca el detector directamente.
        """
        outcome = ReconcileOutcome()
        if not desired:
            # delta vacío / sin desired: nada que aplicar, re-reporta el estado actual.
            outcome.reported = self.publish_reported(camera_id)
            return outcome
        try:
            config = line_config_from_document(desired, schema=self._schema)
        except LineConfigContractError as exc:
            outcome.rejected_contract = True
            _log.error(
                "shadow-reconciler: desired de %s NO casa el contrato (%s); NO se aplica",
                camera_id,
                "; ".join(exc.reasons),
            )
            outcome.reported = self.publish_reported(camera_id)
            return outcome

        if config.camera_id != camera_id:
            outcome.rejected_camera = True
            _log.error(
                "shadow-reconciler: desired camera_id=%r no casa el shadow de %r; NO se aplica",
                config.camera_id,
                camera_id,
            )
            outcome.reported = self.publish_reported(camera_id)
            return outcome

        new_version = self._store.apply_remote_line_config(camera_id, config)
        if new_version is None:
            outcome.ignored_stale = True
            _log.info(
                "shadow-reconciler: desired de %s (v%s) <= actual; ignorado, re-reporto",
                camera_id,
                config.config_version,
            )
        else:
            outcome.applied = True
            outcome.new_version = new_version
            _log.info(
                "shadow-reconciler: %s -> config_version=%d aplicado en SQLite "
                "(ConfigWatcher recargará en caliente)",
                camera_id,
                new_version,
            )
        outcome.reported = self.publish_reported(camera_id)
        return outcome

    def publish_reported(self, camera_id: str) -> bool:
        """Publica ``reported`` (la config local vigente) en el shadow de la cámara.

        Devuelve ``True`` si publicó. Si no hay config local todavía, no publica
        (nada que reportar). Actualiza la versión reportada cacheada.
        """
        config = self._store.get_line_config(camera_id)
        if config is None:
            return False
        doc = line_config_to_document(config)
        topic = named_shadow_topic(
            self._thing_name, line_config_shadow_name(camera_id), "update"
        )
        client = self._ensure_client()
        client.publish(
            topic,
            _encode({"state": {"reported": doc}}),
            qos=QOS_AT_LEAST_ONCE,
            retain=False,
        )
        with self._lock:
            self._reported_version[camera_id] = int(config.config_version)
        return True

    # -- tick periódico: detecta ediciones de la UI local -> reported -----

    def tick(self) -> list[str]:
        """Publica ``reported`` de las cámaras cuyo ``config_version`` cambió en SQLite.

        Sondea barato (un ``SELECT`` por cámara) el ``config_version`` y, si difiere
        del último reportado (p.ej. porque la **UI local** editó la línea), publica
        ``reported``. Así la edición local se refleja en el shadow SIN que la UI
        cambie (el SQLite sigue siendo el único punto de aplicación). Devuelve las
        cámaras re-reportadas.
        """
        changed: list[str] = []
        for camera_id in self._camera_ids:
            try:
                current = int(self._store.get_config_version(camera_id))
            except Exception as exc:  # noqa: BLE001
                _log.debug("shadow-reconciler: tick get_config_version(%s) (%r)", camera_id, exc)
                continue
            with self._lock:
                last = self._reported_version.get(camera_id)
            if last is None or current != last:
                if self.publish_reported(camera_id):
                    changed.append(camera_id)
        return changed

    # -- comandos ---------------------------------------------------------

    def _run_command(self, command: dict[str, Any], *, source: str) -> dict[str, Any]:
        """Despacha un comando por el handler (idempotente) y devuelve el ack."""
        if self._command_handler is None:
            ack = {
                "command_id": command.get("command_id", ""),
                "action": command.get("action"),
                "status": "rejected",
                "error": "device sin command handler configurado",
            }
        else:
            ack = self._command_handler.handle(command)
        _log.info("shadow-reconciler: comando (%s) %s -> %s",
                  source, ack.get("command_id"), ack.get("status"))
        return ack

    def _handle_command_request(self, command: dict[str, Any]) -> dict[str, Any]:
        """Fire-and-forget: ejecuta el comando y publica el ack en ``cmd/ack``."""
        ack = self._run_command(command, source="cmd/request")
        client = self._ensure_client()
        client.publish(
            self._cmd_ack_topic, _encode(ack), qos=QOS_AT_LEAST_ONCE, retain=False
        )
        return ack

    def _handle_command_shadow(self, desired: dict[str, Any]) -> dict[str, Any] | None:
        """Persistente (delta del shadow ``command``): ejecuta y reporta el ack."""
        if not desired or "command_id" not in desired:
            return None
        ack = self._run_command(desired, source="shadow:command")
        self._report_command(ack)
        return ack

    def _handle_command_get_accepted(self, doc: dict[str, Any]) -> dict[str, Any] | None:
        """Boot del shadow ``command``: marca lo ya reportado y ejecuta el desired pendiente.

        El ``reported.command_id`` que llega indica un comando YA ejecutado: se
        siembra en el handler para no re-ejecutarlo tras un ``restart``. Luego, si
        hay un ``desired`` con un ``command_id`` distinto, se ejecuta (llegó offline).
        """
        state = doc.get("state")
        if not isinstance(state, dict):
            return None
        reported = state.get("reported")
        if isinstance(reported, dict) and self._command_handler is not None:
            rid = reported.get("command_id")
            if isinstance(rid, str) and rid and not self._command_handler.is_handled(rid):
                self._command_handler.register_seen(rid)
        desired = state.get("desired")
        if isinstance(desired, dict) and desired.get("command_id"):
            ack = self._run_command(desired, source="shadow:command:boot")
            self._report_command(ack)
            return ack
        return None

    def _report_command(self, ack: dict[str, Any]) -> None:
        """Publica el ack como ``reported`` del shadow ``command`` (converge el delta)."""
        topic = named_shadow_topic(self._thing_name, COMMAND_SHADOW, "update")
        client = self._ensure_client()
        client.publish(
            topic,
            _encode({"state": {"reported": ack}}),
            qos=QOS_AT_LEAST_ONCE,
            retain=False,
        )

    # -- ciclo de vida ----------------------------------------------------

    def close(self) -> None:
        """Para el loop y desconecta limpio (best-effort)."""
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            _log.debug("shadow-reconciler: error al desconectar (%r)", exc)


def _encode(payload: dict[str, Any]) -> bytes:
    """Serializa a bytes JSON compactos y deterministas (UTF-8)."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


# --------------------------------------------------------------------------- #
# Entrypoint standalone (reusa la identidad mTLS del device, como el publicador)
# --------------------------------------------------------------------------- #


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _build_command_handler() -> CommandHandler:
    """Construye un ``CommandHandler`` con las acciones disponibles en el device.

    ``reload-config`` es seguro y útil (fuerza re-reporte); ``snapshot`` y
    ``restart`` se registran como no-ops observables por defecto (el agente
    unificado de un WP posterior inyectará las acciones reales). El handler es
    idempotente por ``command_id`` pase lo que pase.
    """
    def _noop(action: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def _fn(_args: dict[str, Any]) -> dict[str, Any]:
            _log.info("shadow-reconciler: acción %s solicitada (no-op por defecto)", action)
            return {"action": action, "note": "no-op (sin handler real wired)"}
        return _fn

    return CommandHandler(
        {
            "reload-config": _noop("reload-config"),
            "snapshot": _noop("snapshot"),
            "restart": _noop("restart"),
        }
    )


def main(argv: list[str] | None = None) -> int:
    """Bucle del reconciliador de Device Shadow (standalone). Devuelve 0 al parar.

    Lee la identidad IoT del entorno (``CAMCOUNTER_IOT_*`` que escribe
    provision-device.sh) y reusa la misma identidad mTLS del publicador. Es
    edge-first: nunca muere por un fallo de red.
    """
    logging.basicConfig(level=logging.INFO)

    db_path = _env("CAMCOUNTER_DB_PATH", "cam-counter.db")
    device_id = _env("CAMCOUNTER_DEVICE_ID", "demo-pi")
    thing_name = _env("CAMCOUNTER_IOT_THING_NAME") or _env("CAMCOUNTER_IOT_CLIENT_ID")
    endpoint = _env("CAMCOUNTER_IOT_ENDPOINT")
    if not thing_name or not endpoint:
        _log.error(
            "shadow-reconciler: faltan CAMCOUNTER_IOT_THING_NAME/CAMCOUNTER_IOT_ENDPOINT; "
            "provisiona el device (scripts/provision-device.sh) antes del modo shadow."
        )
        return 2

    cams_env = _env("CAMCOUNTER_CAMERA_IDS").strip()
    camera_ids = [c.strip() for c in cams_env.split(",") if c.strip()] or None

    try:
        interval_s = max(2.0, float(_env("CAMCOUNTER_SHADOW_INTERVAL_S", "10")))
    except ValueError:
        interval_s = 10.0

    # Import perezoso del factory de paho (misma fuente que el publicador WP14).
    from .mqtt_publisher import default_mqtt_client_factory  # noqa: PLC0415

    store = Store(db_path)
    reconciler = ShadowReconciler(
        store,
        thing_name=thing_name,
        device_id=device_id,
        camera_ids=camera_ids,
        # paho real (mismo factory que el publicador WP14); su _MqttClientLike es
        # estructuralmente compatible (subscribe/publish/connect/loop_*), pero son
        # Protocols distintos en cada módulo, de ahí el ignore.
        client_factory=default_mqtt_client_factory,  # type: ignore[arg-type]
        command_handler=_build_command_handler(),
        endpoint=endpoint,
        cert_path=_env("CAMCOUNTER_IOT_CERT_PATH"),
        key_path=_env("CAMCOUNTER_IOT_KEY_PATH"),
        ca_path=_env("CAMCOUNTER_IOT_ROOT_CA_PATH"),
        app_version=_env("CAMCOUNTER_APP_VERSION", "edge-dev"),
    )

    stop = threading.Event()

    def _handle(_signum: int, _frame: Any) -> None:
        _log.info("shadow-reconciler: señal recibida; parando…")
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    _log.info(
        "shadow-reconciler: thing=%s device=%s endpoint=%s cámaras=%s intervalo=%ss",
        thing_name,
        device_id,
        endpoint,
        reconciler.camera_ids,
        interval_s,
    )

    try:
        reconciler.connect()
    except Exception as exc:  # noqa: BLE001 — edge-first: no morir por fallo de conexión
        _log.warning("shadow-reconciler: conexión inicial falló (%r); paho reintentará", exc)

    while not stop.is_set():
        try:
            changed = reconciler.tick()
            if changed:
                _log.info("shadow-reconciler: re-reportadas por edición local: %s", changed)
        except Exception as exc:  # noqa: BLE001 — el reconciliador NUNCA debe morir
            _log.warning("shadow-reconciler: error en el tick (%r); reintento luego", exc)
        stop.wait(interval_s)

    reconciler.close()
    store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
