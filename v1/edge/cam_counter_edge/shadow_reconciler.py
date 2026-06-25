"""Reconciliador de Device Shadow (canal comando/config nube->dispositivo).

Implementa el canal **nube->dispositivo** del WP15 sobre AWS IoT Core **Device
Shadow**, apilado sobre el publicador MQTT (WP14): reutiliza la MISMA sesión mTLS
(el cliente paho es INYECTABLE, igual que en ``mqtt_publisher``) para suscribirse a
los shadows y publicar ``reported``.

Dos canales coexisten:

1. **Config de línea** (named shadow ``line-config-{camera_id}``, uno POR CÁMARA):
   el ``line_config`` es POR-CÁMARA (el contrato EXIGE ``camera_id``) pero el shadow
   es por-THING, así que se resuelven N cámaras con un named shadow por cámara.
   El ``desired`` que escribe la nube ES un ``line_config`` del contrato canónico
   (``contracts/line_config.schema.json``). Al arrancar se hace ``.../get`` para
   sincronizar el ``desired`` aunque el cambio ocurriera offline; en caliente se
   reacciona al ``.../update/delta``. El delta se valida **VERBATIM** contra el
   contrato (**falla cerrada**: un desired inválido NO se aplica).

2. **Comandos** (fire-and-forget vía ``cam-counter/{device_id}/cmd/request`` ->
   ``.../cmd/ack``; persistentes vía named shadow ``command``). Se ejecutan de forma
   **IDEMPOTENTE** por ``command_id``: un mismo ``command_id`` se ejecuta UNA vez y
   re-acka sin re-ejecutar.

**Sin split-brain (GUARDARRAIL):** el SQLite es el ÚNICO punto de aplicación y su
``config_version`` el árbitro único. El reconciliador NO toca la geometría en vivo
del detector: escribe en SQLite (vía ``store.apply_remote_line_config``, "gana la
versión mayor") y el ``ConfigWatcher`` existente recarga la línea EN CALIENTE al ver
un ``config_version`` mayor (mismo mecanismo que la UI local). Cuando la UI local
edita el SQLite, se publica ``reported`` para que la nube se entere.

paho-mqtt es opcional (extra ``iot``) y se importa de forma PEREZOSA; la lógica de
reconciliación/validación/idempotencia se ejercita en CI x86 con un cliente fake.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from .identifiers import validate_camera_id, validate_device_id
from .types import Line, LineConfig, Point

__all__ = [
    "CommandOutcome",
    "ReconcileOutcome",
    "ShadowReconciler",
    "command_shadow_base",
    "line_config_doc",
    "line_config_shadow_base",
    "line_config_shadow_name",
    "load_line_config_schema",
    "validate_line_config",
    "main",
]

_log = logging.getLogger(__name__)

# QoS1 (at-least-once): junto con la idempotencia da exactly-once efectivo.
QOS_AT_LEAST_ONCE = 1

# Prefijo de topic de comandos canónico (igual canon que provision-device.sh:
# CAMCOUNTER_IOT_TOPIC_CMD == cam-counter/{device_id}/cmd).
PRODUCT_PREFIX = "cam-counter"
CMD_SUFFIX = "cmd"

# Named shadow de config de línea (uno POR CÁMARA) y de comandos persistentes.
LINE_CONFIG_SHADOW_PREFIX = "line-config"
COMMAND_SHADOW_NAME = "command"

# Acciones de comando soportadas (canon WP15).
ACTION_SNAPSHOT = "snapshot"
ACTION_RESTART = "restart"
ACTION_RELOAD_CONFIG = "reload-config"
SUPPORTED_ACTIONS = (ACTION_SNAPSHOT, ACTION_RESTART, ACTION_RELOAD_CONFIG)

# Override explícito de la ruta del contrato (tests / empaquetado).
_SCHEMA_ENV = "CAMCOUNTER_LINE_CONFIG_SCHEMA_PATH"
_SCHEMA_NAME = "line_config.schema.json"


# --------------------------------------------------------------------------- #
# Carga + validación VERBATIM del contrato line_config (subset Draft 2020-12)
# --------------------------------------------------------------------------- #


def _find_schema_path() -> Path:
    """Resuelve la ruta del contrato: override -> árbol del repo ``contracts/``."""
    override = os.environ.get(_SCHEMA_ENV)
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / _SCHEMA_NAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"no se encontró contracts/{_SCHEMA_NAME}; define {_SCHEMA_ENV} para apuntarlo."
    )


@lru_cache(maxsize=2)
def load_line_config_schema(path: str | None = None) -> dict[str, Any]:
    """Carga (cacheado) el JSON Schema canónico de ``line_config``."""
    schema_path = Path(path) if path is not None else _find_schema_path()
    return json.loads(schema_path.read_text(encoding="utf-8"))


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "object": dict,
    "array": list,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, type_spec: Any) -> bool:
    specs = type_spec if isinstance(type_spec, list) else [type_spec]
    for spec in specs:
        py = _JSON_TYPES.get(spec)
        if py is None:
            continue
        # bool es subclase de int en Python; el contrato no usa booleanos numéricos.
        if spec in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def _validate(value: Any, schema: dict[str, Any], path: str, reasons: list[str]) -> None:
    """Valida ``value`` contra ``schema`` (subset Draft 2020-12), RECURSIVO.

    Soporta el subconjunto EXACTO que usa ``line_config.schema.json``: ``type``
    (incl. unión), ``enum``, ``const``, ``pattern``, ``minimum``/``maximum``,
    ``required``, ``additionalProperties:false`` y ``properties`` ANIDADAS (objeto
    ``line`` con endpoints ``a``/``b``). NO importa ``jsonschema`` (misma filosofía
    que ``crossing_payload`` y la Lambda de ingesta).
    """
    loc = path or "(raíz)"
    if "type" in schema and not _type_ok(value, schema["type"]):
        reasons.append(f"{loc}: tipo inválido (esperado {schema['type']})")
        return  # sin el tipo correcto, las demás aserciones no aplican
    if "const" in schema and value != schema["const"]:
        reasons.append(f"{loc}: debe ser {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        reasons.append(f"{loc}: valor fuera de enum {schema['enum']}")
    if "pattern" in schema and isinstance(value, str):
        if not re.search(schema["pattern"], value):
            reasons.append(f"{loc}: no casa el patrón {schema['pattern']!r}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            reasons.append(f"{loc}: menor que el mínimo {schema['minimum']}")
    if "maximum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > schema["maximum"]:
            reasons.append(f"{loc}: mayor que el máximo {schema['maximum']}")
    if schema.get("type") == "object" and isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    reasons.append(f"{loc}: propiedad no permitida: {key!r}")
        for req in schema.get("required", []):
            if req not in value:
                reasons.append(f"{loc}: falta campo requerido: {req!r}")
        for key, subval in value.items():
            subschema = props.get(key)
            if subschema is not None:
                child = f"{path}.{key}" if path else key
                _validate(subval, subschema, child, reasons)


def validate_line_config(doc: Any, schema: dict[str, Any] | None = None) -> list[str]:
    """Valida ``doc`` contra el contrato ``line_config`` VERBATIM. Lista de motivos.

    Lista vacía == válido. Se usa **fail-closed**: si devuelve motivos, el desired
    NO se aplica (ver ``ShadowReconciler``).
    """
    if not isinstance(doc, dict):
        return [f"(raíz): tipo inválido (esperado object), es {type(doc).__name__}"]
    schema = schema if schema is not None else load_line_config_schema()
    reasons: list[str] = []
    _validate(doc, schema, "", reasons)
    return reasons


# --------------------------------------------------------------------------- #
# Serialización LineConfig <-> documento del contrato (shadow state)
# --------------------------------------------------------------------------- #


def line_config_doc(cfg: LineConfig) -> dict[str, Any]:
    """``LineConfig`` -> documento ``line_config`` del contrato (para ``reported``).

    Incluye los requeridos + opcionales con valor (omite ``None``). El resultado
    casa el contrato VERBATIM.
    """
    doc: dict[str, Any] = {
        "site_id": cfg.site_id,
        "device_id": cfg.device_id,
        "camera_id": cfg.camera_id,
        "config_version": int(cfg.config_version),
        "line": {
            "a": {"x": float(cfg.line.a.x), "y": float(cfg.line.a.y)},
            "b": {"x": float(cfg.line.b.x), "y": float(cfg.line.b.y)},
        },
        "positive_side": int(cfg.positive_side),
        "schema_version": int(cfg.schema_version),
    }
    if cfg.positive_label is not None:
        doc["positive_label"] = cfg.positive_label
    if cfg.negative_label is not None:
        doc["negative_label"] = cfg.negative_label
    if cfg.updated_at is not None:
        doc["updated_at"] = cfg.updated_at
    return doc


def _line_config_from_doc(doc: dict[str, Any]) -> LineConfig:
    """Documento ``line_config`` (YA validado) -> ``LineConfig``."""
    line = doc["line"]
    return LineConfig(
        site_id=doc["site_id"],
        device_id=doc["device_id"],
        camera_id=doc["camera_id"],
        config_version=int(doc["config_version"]),
        line=Line(
            a=Point(float(line["a"]["x"]), float(line["a"]["y"])),
            b=Point(float(line["b"]["x"]), float(line["b"]["y"])),
        ),
        positive_side=int(doc["positive_side"]),
        positive_label=doc.get("positive_label"),
        negative_label=doc.get("negative_label"),
        updated_at=doc.get("updated_at"),
        schema_version=int(doc.get("schema_version", 1)),
    )


# --------------------------------------------------------------------------- #
# Topics de shadow (named shadow por cámara / por comando)
# --------------------------------------------------------------------------- #


def line_config_shadow_name(camera_id: str) -> str:
    """Named shadow de config de línea para una cámara: ``line-config-{camera_id}``."""
    validate_camera_id(camera_id)
    return f"{LINE_CONFIG_SHADOW_PREFIX}-{camera_id}"


def line_config_shadow_base(thing_name: str, camera_id: str) -> str:
    """Prefijo de topics del named shadow de config de una cámara."""
    name = line_config_shadow_name(camera_id)
    return f"$aws/things/{thing_name}/shadow/name/{name}"


def command_shadow_base(thing_name: str) -> str:
    """Prefijo de topics del named shadow de comandos persistentes."""
    return f"$aws/things/{thing_name}/shadow/name/{COMMAND_SHADOW_NAME}"


# --------------------------------------------------------------------------- #
# Cliente MQTT mínimo (inyectable; paho-compatible) y resultados
# --------------------------------------------------------------------------- #


class _ShadowClientLike(Protocol):
    """Subconjunto de ``paho.mqtt.client.Client`` que usa el reconciliador."""

    on_message: Any

    def subscribe(self, topic: str, qos: int = ...) -> Any: ...
    def publish(self, topic: str, payload: Any, qos: int = ..., retain: bool = ...) -> Any: ...


class ReconcileOutcome:
    """Resultado de reconciliar un desired de ``line-config`` (observabilidad/tests)."""

    def __init__(self) -> None:
        self.applied = False  # se escribió una versión mayor en SQLite
        self.ignored_stale = False  # desired <= local (re-reportado)
        self.rejected_contract = False  # desired no casó el contrato (fail-closed)
        self.reported = False  # se publicó reported
        self.new_version: int | None = None
        self.reasons: list[str] = []


class CommandOutcome:
    """Resultado de procesar un comando (observabilidad/tests)."""

    def __init__(self) -> None:
        self.command_id: str | None = None
        self.action: str | None = None
        self.status: str = "ignored"  # ok | duplicate | unsupported | rejected | error
        self.executed = False  # el handler corrió en ESTA invocación
        self.acked = False
        self.result: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Reconciliador
# --------------------------------------------------------------------------- #


class ShadowReconciler:
    """Reconcilia Device Shadows (config de línea + comandos) contra el SQLite local.

    Args:
        store: capa SQLite del borde (fuente de verdad y ÚNICO punto de aplicación;
            expone ``apply_remote_line_config`` y ``get_line_config``).
        thing_name: nombre del Thing IoT (los topics de shadow derivan de él).
        device_id: slug del device; de él DERIVA el topic de comandos.
        camera_ids: cámaras del device (un named shadow de config POR cámara).
        client: cliente MQTT inyectado (fake en CI); puede ser el MISMO de
            ``MqttPublisher`` para reutilizar la sesión mTLS. Si ``None``, se opera
            en modo "manual" (los tests llaman ``handle_message`` directamente).
        command_handlers: mapa ``action -> callable(command) -> dict`` con la lógica
            de aplicación de ``snapshot``/``restart`` (efectos a nivel de proceso).
            ``reload-config`` tiene un handler por defecto (re-sincroniza shadows).
        app_version: versión reportada en los acks.
    """

    def __init__(
        self,
        store: Any,
        *,
        thing_name: str,
        device_id: str,
        camera_ids: list[str],
        client: _ShadowClientLike | None = None,
        command_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        app_version: str = "edge-dev",
    ) -> None:
        if not thing_name:
            raise ValueError("thing name vacío: no se pueden derivar los topics de shadow")
        self._store = store
        self._thing_name = thing_name
        self._device_id = validate_device_id(device_id)
        self._camera_ids = [validate_camera_id(c) for c in camera_ids]
        self._client = client
        self._command_handlers = dict(command_handlers or {})
        self._app_version = app_version

        self._cmd_prefix = f"{PRODUCT_PREFIX}/{self._device_id}/{CMD_SUFFIX}"
        self._cmd_request_topic = f"{self._cmd_prefix}/request"
        self._cmd_ack_topic = f"{self._cmd_prefix}/ack"

        # Índice topic -> camera_id para enrutar mensajes de shadow de config.
        self._delta_topic_to_cam: dict[str, str] = {}
        self._get_accepted_topic_to_cam: dict[str, str] = {}
        for cam in self._camera_ids:
            base = line_config_shadow_base(self._thing_name, cam)
            self._delta_topic_to_cam[f"{base}/update/delta"] = cam
            self._get_accepted_topic_to_cam[f"{base}/get/accepted"] = cam

        self._cmd_shadow_base = command_shadow_base(self._thing_name)

        # Idempotencia de comandos: command_id -> ack ya emitido (cacheado).
        self._processed_commands: dict[str, dict[str, Any]] = {}

    # -- topics (lectura para tests/observabilidad) ----------------------

    @property
    def cmd_request_topic(self) -> str:
        return self._cmd_request_topic

    @property
    def cmd_ack_topic(self) -> str:
        return self._cmd_ack_topic

    # -- ciclo de vida: suscripción + sincronización de arranque ----------

    def attach(self, client: _ShadowClientLike | None = None) -> None:
        """Registra el dispatcher ``on_message``, se suscribe y sincroniza al boot.

        Reutiliza el cliente inyectado (o el pasado aquí). Tras suscribirse, dispara
        ``request_get_all`` para sincronizar el ``desired`` aunque el cambio ocurriera
        OFFLINE (boot hace shadow get).
        """
        if client is not None:
            self._client = client
        self.subscribe_all()
        self.request_get_all()

    def _require_client(self) -> _ShadowClientLike:
        if self._client is None:
            raise RuntimeError(
                "ShadowReconciler sin cliente MQTT: inyecta uno (el de MqttPublisher "
                "para reusar la sesión mTLS) o usa el modo manual (handle_message)."
            )
        return self._client

    def subscribe_all(self) -> None:
        """Suscribe a delta/get-accepted de cada shadow de config + comandos."""
        client = self._require_client()
        client.on_message = self._on_message
        for cam in self._camera_ids:
            base = line_config_shadow_base(self._thing_name, cam)
            client.subscribe(f"{base}/update/delta", QOS_AT_LEAST_ONCE)
            client.subscribe(f"{base}/get/accepted", QOS_AT_LEAST_ONCE)
        # Comandos: fire-and-forget (cmd/request) + persistentes (shadow command).
        client.subscribe(self._cmd_request_topic, QOS_AT_LEAST_ONCE)
        client.subscribe(f"{self._cmd_shadow_base}/update/delta", QOS_AT_LEAST_ONCE)
        client.subscribe(f"{self._cmd_shadow_base}/get/accepted", QOS_AT_LEAST_ONCE)

    def request_get_all(self) -> None:
        """Publica ``.../get`` de cada shadow para sincronizar el desired al arrancar."""
        client = self._require_client()
        for cam in self._camera_ids:
            base = line_config_shadow_base(self._thing_name, cam)
            client.publish(f"{base}/get", b"", QOS_AT_LEAST_ONCE, False)
        client.publish(f"{self._cmd_shadow_base}/get", b"", QOS_AT_LEAST_ONCE, False)

    # -- dispatch de mensajes --------------------------------------------

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Callback paho: extrae ``topic``/``payload`` y delega en ``handle_message``."""
        try:
            self.handle_message(message.topic, message.payload)
        except Exception as exc:  # noqa: BLE001 — el reconciliador NUNCA debe morir
            _log.warning("shadow-reconciler: error procesando mensaje (%r)", exc)

    def handle_message(self, topic: str, payload: bytes | str | dict[str, Any]) -> None:
        """Enruta un mensaje de shadow/comando por topic (entrypoint testeable).

        Acepta ``payload`` como bytes/str JSON o dict ya parseado (cómodo en tests).
        """
        doc = self._parse_payload(payload)
        if doc is None:
            _log.warning("shadow-reconciler: payload no-JSON en %s; ignorado", topic)
            return

        cam = self._delta_topic_to_cam.get(topic)
        if cam is not None:
            # update/delta: el ``state`` ES el desired que cambió (full line_config).
            self._reconcile_line_config(cam, doc.get("state"), source="delta")
            return
        cam = self._get_accepted_topic_to_cam.get(topic)
        if cam is not None:
            # get/accepted: el desired vive en ``state.desired``.
            desired = (doc.get("state") or {}).get("desired")
            self._reconcile_line_config(cam, desired, source="get")
            return

        if topic == self._cmd_request_topic:
            self.handle_command(doc)
            return
        if topic == f"{self._cmd_shadow_base}/update/delta":
            self.handle_command(doc.get("state"))
            return
        if topic == f"{self._cmd_shadow_base}/get/accepted":
            desired = (doc.get("state") or {}).get("desired")
            self.handle_command(desired)
            return

        _log.debug("shadow-reconciler: topic no reconocido %s; ignorado", topic)

    @staticmethod
    def _parse_payload(payload: bytes | str | dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            return payload
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8")
            parsed = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # -- reconciliación de config de línea (nube -> SQLite) ---------------

    def _reconcile_line_config(
        self, camera_id: str, desired: Any, *, source: str
    ) -> ReconcileOutcome:
        """Aplica un desired de ``line-config`` en SQLite (gana la versión mayor).

        Valida VERBATIM (fail-closed). Si ``desired.config_version > local`` escribe
        en SQLite (el ``ConfigWatcher`` recargará la línea en caliente) y reporta; si
        no, ignora y RE-REPORTA el estado local (para que la nube se ponga al día).
        """
        outcome = ReconcileOutcome()
        if desired is None:
            # Shadow sin desired (p.ej. get/accepted de un shadow recién creado):
            # nada que aplicar, pero re-reportamos lo local si lo hay.
            self._publish_reported(camera_id, outcome)
            return outcome

        reasons = validate_line_config(desired)
        if reasons:
            outcome.rejected_contract = True
            outcome.reasons = reasons
            _log.error(
                "shadow-reconciler: desired de %s (%s) NO casa el contrato "
                "line_config; NO se aplica (fail-closed): %s",
                camera_id,
                source,
                "; ".join(reasons),
            )
            return outcome

        # El desired debe ser de ESTA cámara (anti-mismatch entre shadow y contrato).
        if desired.get("camera_id") != camera_id:
            outcome.rejected_contract = True
            outcome.reasons = [
                f"camera_id del desired ({desired.get('camera_id')!r}) != shadow "
                f"({camera_id!r})"
            ]
            _log.error(
                "shadow-reconciler: %s; desired descartado (fail-closed)",
                outcome.reasons[0],
            )
            return outcome

        config = _line_config_from_doc(desired)
        new_version = self._store.apply_remote_line_config(config)
        if new_version is not None:
            outcome.applied = True
            outcome.new_version = new_version
            _log.info(
                "shadow-reconciler: %s aplicó line-config v%d desde la nube (%s); "
                "el ConfigWatcher recargará en caliente",
                camera_id,
                new_version,
                source,
            )
        else:
            outcome.ignored_stale = True
            _log.info(
                "shadow-reconciler: desired de %s (v%d) <= local; ignorado, re-reporto",
                camera_id,
                config.config_version,
            )
        self._publish_reported(camera_id, outcome)
        return outcome

    def publish_reported(self, camera_id: str) -> bool:
        """Publica el ``reported`` de la config local de una cámara (API pública).

        La llama el bucle que detecta ediciones de la UI local (tarea WP15 #3): tras
        un cambio en SQLite, se reporta para que la nube vea el ``config_version``
        actual del dispositivo. Devuelve ``True`` si publicó.
        """
        validate_camera_id(camera_id)
        outcome = ReconcileOutcome()
        self._publish_reported(camera_id, outcome)
        return outcome.reported

    def _publish_reported(self, camera_id: str, outcome: ReconcileOutcome) -> None:
        if self._client is None:
            return
        cfg = self._store.get_line_config(camera_id)
        if cfg is None:
            return  # nada local que reportar todavía
        base = line_config_shadow_base(self._thing_name, camera_id)
        payload = json.dumps(
            {"state": {"reported": line_config_doc(cfg)}},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._client.publish(f"{base}/update", payload, QOS_AT_LEAST_ONCE, False)
        outcome.reported = True

    # -- comandos (idempotentes por command_id) ---------------------------

    def handle_command(self, command: Any) -> CommandOutcome:
        """Procesa un comando (fire-and-forget o persistente). IDEMPOTENTE.

        Un mismo ``command_id`` se ejecuta UNA vez; las repeticiones re-publican el
        MISMO ack sin re-ejecutar. Tras ejecutar, reporta el ``command_id`` al shadow
        ``command`` (limpia el delta) y publica el ack en ``cmd/ack``.
        """
        outcome = CommandOutcome()
        if not isinstance(command, dict):
            outcome.status = "rejected"
            return outcome
        command_id = command.get("command_id")
        action = command.get("action")
        outcome.command_id = command_id if isinstance(command_id, str) else None
        outcome.action = action if isinstance(action, str) else None

        if not isinstance(command_id, str) or not command_id:
            outcome.status = "rejected"
            _log.error("shadow-reconciler: comando sin command_id válido; descartado")
            return outcome

        # Idempotencia: si ya se procesó, re-ackear con el resultado cacheado.
        cached = self._processed_commands.get(command_id)
        if cached is not None:
            outcome.status = "duplicate"
            outcome.result = cached.get("result")
            outcome.action = cached.get("action")
            self._ack_command(command_id, cached, outcome)
            _log.info(
                "shadow-reconciler: comando %s duplicado; re-ack sin re-ejecutar",
                command_id,
            )
            return outcome

        if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
            outcome.status = "unsupported"
            ack = {"status": "unsupported", "action": action, "result": None}
            self._processed_commands[command_id] = ack
            self._ack_command(command_id, ack, outcome)
            _log.warning(
                "shadow-reconciler: acción no soportada %r (cmd %s)", action, command_id
            )
            return outcome

        # Ejecuta el handler de la acción (idempotente a nivel de proceso).
        handler = self._command_handlers.get(action) or self._default_handler(action)
        try:
            result = handler(command) if handler is not None else None
            outcome.status = "ok"
            outcome.executed = True
            outcome.result = result if isinstance(result, dict) else None
        except Exception as exc:  # noqa: BLE001 — un handler no debe matar el bucle
            outcome.status = "error"
            outcome.result = {"error": repr(exc)}
            _log.warning(
                "shadow-reconciler: handler de %s (cmd %s) falló (%r)",
                action,
                command_id,
                exc,
            )

        ack = {"status": outcome.status, "action": action, "result": outcome.result}
        self._processed_commands[command_id] = ack
        self._ack_command(command_id, ack, outcome)
        return outcome

    def _default_handler(
        self, action: str
    ) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
        """Handler por defecto: ``reload-config`` re-sincroniza shadows (re-get)."""
        if action == ACTION_RELOAD_CONFIG:

            def _reload(_command: dict[str, Any]) -> dict[str, Any]:
                # Re-pide el desired y re-reporta lo local de cada cámara. El detector
                # recarga vía ConfigWatcher; aquí sólo re-sincronizamos los shadows.
                if self._client is not None:
                    self.request_get_all()
                    for cam in self._camera_ids:
                        self.publish_reported(cam)
                return {"reloaded_cameras": list(self._camera_ids)}

            return _reload
        # snapshot / restart requieren efectos de proceso: sin handler inyectado, el
        # comando se acka como 'unsupported' (no se inventa el efecto).
        return None

    def _ack_command(
        self, command_id: str, ack: dict[str, Any], outcome: CommandOutcome
    ) -> None:
        """Publica el ack en ``cmd/ack`` y reporta el ``command_id`` al shadow command."""
        if self._client is None:
            return
        ack_payload = {
            "command_id": command_id,
            "device_id": self._device_id,
            "status": ack.get("status"),
            "action": ack.get("action"),
            "agent_version": self._app_version,
            "ts_ms": int(time.time() * 1000),
        }
        if ack.get("result") is not None:
            ack_payload["result"] = ack["result"]
        self._client.publish(
            self._cmd_ack_topic,
            json.dumps(ack_payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            QOS_AT_LEAST_ONCE,
            False,
        )
        # Reporta el command_id procesado al shadow persistente (limpia el delta).
        reported = {"state": {"reported": {"command_id": command_id, "status": ack.get("status")}}}
        self._client.publish(
            f"{self._cmd_shadow_base}/update",
            json.dumps(reported, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            QOS_AT_LEAST_ONCE,
            False,
        )
        outcome.acked = True


# --------------------------------------------------------------------------- #
# Entrypoint standalone (paho real; import PEREZOSO del extra `iot`)
# --------------------------------------------------------------------------- #


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - I/O real
    """Entrypoint standalone del reconciliador de shadows (modo ``iot``).

    Para producción se recomienda COMPARTIR el cliente del ``MqttPublisher`` (una
    sola sesión mTLS); este ``main`` levanta su PROPIO cliente paho para ejecutar el
    reconciliador de forma aislada (útil para depurar). Edge-first: nunca muere por
    un fallo de red.
    """
    import signal  # noqa: PLC0415
    import threading  # noqa: PLC0415

    logging.basicConfig(level=logging.INFO)

    from .store import Store  # noqa: PLC0415

    db_path = _env("CAMCOUNTER_DB_PATH", "cam-counter.db")
    device_id = _env("CAMCOUNTER_DEVICE_ID", "demo-pi")
    thing_name = _env("CAMCOUNTER_IOT_THING_NAME") or _env("CAMCOUNTER_IOT_CLIENT_ID")
    endpoint = _env("CAMCOUNTER_IOT_ENDPOINT")
    cameras_env = _env("CAMCOUNTER_CAMERA_IDS")
    camera_ids = [c.strip() for c in cameras_env.split(",") if c.strip()]
    if not thing_name or not endpoint or not camera_ids:
        _log.error(
            "shadow-reconciler: faltan CAMCOUNTER_IOT_THING_NAME/ENDPOINT o "
            "CAMCOUNTER_CAMERA_IDS (csv); provisiona el device antes de modo iot."
        )
        return 2

    from .mqtt_publisher import default_mqtt_client_factory  # noqa: PLC0415

    client = default_mqtt_client_factory(thing_name)
    if hasattr(client, "tls_set"):
        cert = _env("CAMCOUNTER_IOT_CERT_PATH")
        key = _env("CAMCOUNTER_IOT_KEY_PATH")
        ca = _env("CAMCOUNTER_IOT_ROOT_CA_PATH")
        if ca or cert or key:
            client.tls_set(ca_certs=ca or None, certfile=cert or None, keyfile=key or None)

    store = Store(db_path)
    reconciler = ShadowReconciler(
        store,
        thing_name=thing_name,
        device_id=device_id,
        camera_ids=camera_ids,
        client=client,
        app_version=_env("CAMCOUNTER_APP_VERSION", "edge-dev"),
    )

    stop = threading.Event()

    def _handle(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        port = int(_env("CAMCOUNTER_IOT_PORT", "8883"))
        client.connect(endpoint, port, 60)
        client.loop_start()
        reconciler.attach()
    except Exception as exc:  # noqa: BLE001 — edge-first
        _log.warning("shadow-reconciler: conexión inicial falló (%r); reintentará", exc)

    while not stop.is_set():
        stop.wait(30.0)

    try:
        client.loop_stop()
        client.disconnect()
    except Exception as exc:  # noqa: BLE001
        _log.debug("shadow-reconciler: error al desconectar (%r)", exc)
    store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
