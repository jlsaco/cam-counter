"""Worker de sincronización edge -> cloud (tolerante a offline, manifest-no-registry).

Drena los ``CrossingEvent`` locales aún no sincronizados (``synced=0`` en SQLite)
hacia la nube de forma **idempotente** y **desacoplada del conteo**:

1. Sube el clip del evento al bucket de media por SigV4 (``PutObject``), de forma
   **retry-safe** con ``If-None-Match: *`` para NO pisar un upload previo del mismo
   ``event_id`` en un reintento (el primer escritor gana; un 412 PreconditionFailed
   se trata como "ya subido", éxito idempotente).
2. Hace **conditional-put** del ``CrossingEvent`` en la tabla de eventos con
   ``attribute_not_exists(PK)``. Como el ``event_id`` es DETERMINISTA (sha1 de la
   tupla de identidad), un reintento del MISMO evento es rechazado por la condición
   (``ConditionalCheckFailedException``) y eso NO es error: se marca ``synced=1``
   igual. Esto es lo que hace IDEMPOTENTE el sync (un evento subido dos veces NO
   duplica ni en S3 ni en DynamoDB).
3. Escribe un **heartbeat** al registro de dispositivos SÓLO con ``UpdateItem``
   (``reported_version``, ``last_seen_at``, ``status``). **NUNCA lee el registro**
   para decidir qué subir ni qué versión correr: la única fuente de verdad del
   trabajo pendiente es la cola local SQLite (regla manifest-no-registry).

Filosofía edge-first / tolerante a offline: el conteo y la persistencia local NO
dependen de la red. Si la red está caída, ``sync_once`` deja los eventos sin marcar
(quedan en backlog) y vuelve a intentarlo más tarde con backoff acotado; al
reconectar, el backlog drena en orden y SIN duplicados. El worker NUNCA tumba el
proceso por un fallo de red.

**Clientes AWS inyectables:** ``CloudSync`` recibe los clientes S3 y DynamoDB por
parámetro (boto3 ``client('s3')`` / ``client('dynamodb')`` o equivalentes). Esto
permite que CI los stubee ("red caída luego arriba") sin AWS real, y que la prueba
de integración inyecte clientes boto3 REALES construidos con credenciales STS del
rol per-Pi. Las llamadas usan el FORMATO low-level de DynamoDB (``{'S':..},
{'N':..}``) que aceptan tanto el cliente boto3 real como los fakes de los tests, de
modo que la ruta de código ejercida es EXACTAMENTE la misma con y sin AWS real.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "CloudSync",
    "CloudSyncConfig",
    "SyncOutcome",
    "SyncResult",
    "TransientSyncError",
    "build_event_item",
    "event_keys",
]

_log = logging.getLogger("cam_counter_edge.sync")

# Códigos de error de AWS que tratamos como TRANSITORIOS (red caída, throttling,
# indisponibilidad temporal): el evento queda en backlog y se reintenta. Nunca se
# pierde ni se marca como sincronizado.
_TRANSIENT_ERROR_CODES = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "Throttling",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "InternalServerError",
        "InternalError",
        "RequestTimeout",
        "RequestTimeoutException",
        "SlowDown",
        "TransactionInProgressException",
        "5xx",
    }
)

# Subcadenas en el NOMBRE de la excepción que delatan un problema de red/conexión
# (sin acoplar a botocore: clasificamos por estructura, no por tipo importado).
_TRANSIENT_NAME_HINTS = (
    "EndpointConnectionError",
    "ConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "ConnectionClosedError",
    "HTTPClientError",
)


class TransientSyncError(Exception):
    """Fallo TRANSITORIO (red caída / throttling): reintentar, no perder el evento.

    Los stubs de CI la lanzan para simular "red caída"; ``CloudSync`` la trata como
    backlog (no marca ``synced``, hace backoff). Cualquier excepción cuya forma
    delate un problema de red también se clasifica como transitoria (ver
    ``_is_transient``).
    """


def _error_code(exc: BaseException) -> str | None:
    """Código de error AWS de una excepción estilo botocore ``ClientError``.

    Lee ``exc.response['Error']['Code']`` si existe. Los fakes de los tests imitan
    esa forma para ejercer la MISMA lógica de clasificación que con boto3 real.
    """
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        err = resp.get("Error")
        if isinstance(err, dict):
            code = err.get("Code")
            if isinstance(code, str):
                return code
    return None


def _http_status(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        meta = resp.get("ResponseMetadata")
        if isinstance(meta, dict):
            status = meta.get("HTTPStatusCode")
            if isinstance(status, int):
                return status
    return None


def _is_conditional_check_failed(exc: BaseException) -> bool:
    """¿Es un ``ConditionalCheckFailedException`` (duplicado idempotente)?"""
    return _error_code(exc) == "ConditionalCheckFailedException"


def _is_precondition_failed(exc: BaseException) -> bool:
    """¿Es un 412 PreconditionFailed de S3 (objeto ya existe; ``If-None-Match``)?"""
    return _error_code(exc) == "PreconditionFailed" or _http_status(exc) == 412


def _is_transient(exc: BaseException) -> bool:
    """¿Es un fallo transitorio (red/throttle) que merece reintento sin perder datos?"""
    if isinstance(exc, TransientSyncError):
        return True
    code = _error_code(exc)
    if code is not None and code in _TRANSIENT_ERROR_CODES:
        return True
    name = type(exc).__name__
    if any(hint in name for hint in _TRANSIENT_NAME_HINTS):
        return True
    # botocore.exceptions.EndpointConnectionError y amigos heredan de OSError-ish;
    # un ConnectionError de stdlib también es offline.
    return isinstance(exc, (ConnectionError, TimeoutError))


def _now_iso() -> str:
    """Timestamp ISO-8601 UTC (ms) de pared para metadatos de heartbeat/auditoría."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── serialización low-level de DynamoDB (sin depender de boto3 en CI) ──────────


def _to_attr(value: object) -> dict:
    """Serializa un valor Python a un AttributeValue low-level de DynamoDB."""
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):  # antes que int: bool es subclase de int
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, float):
        return {"N": repr(value)}
    if isinstance(value, str):
        return {"S": value}
    raise TypeError(f"tipo no serializable a DynamoDB: {type(value)!r}")


def _from_attr(attr: dict) -> object:
    """Deserializa un AttributeValue low-level de DynamoDB a un valor Python."""
    if "NULL" in attr:
        return None
    if "BOOL" in attr:
        return bool(attr["BOOL"])
    if "N" in attr:
        text = attr["N"]
        return int(text) if "." not in text and "e" not in text.lower() else float(text)
    if "S" in attr:
        return attr["S"]
    raise TypeError(f"AttributeValue no soportado: {attr!r}")


def _item_to_dynamo(item: dict) -> dict:
    """Convierte un dict Python (valores no-None) a Item low-level de DynamoDB."""
    return {k: _to_attr(v) for k, v in item.items() if v is not None}


def item_from_dynamo(item: dict) -> dict:
    """Convierte un Item low-level de DynamoDB de vuelta a dict Python (read-back)."""
    return {k: _from_attr(v) for k, v in item.items()}


# Campos del contrato CrossingEvent que SÍ viajan a la nube. ``synced`` es un flag
# SÓLO-LOCAL (ver contracts/crossing_event.schema.json) y NO se persiste en DynamoDB.
_CLOUD_EVENT_FIELDS = (
    "event_id",
    "site_id",
    "device_id",
    "camera_id",
    "track_id",
    "crossing_seq",
    "direction",
    "positive_label",
    "negative_label",
    "label",
    "line_version",
    "ts_event_ms",
    "ts_event_iso",
    "confidence",
    "clip_key",
    "clip_status",
    "schema_version",
    "created_at",
)


def event_keys(event: dict) -> dict:
    """Claves canónicas DynamoDB de un evento (PK/SK) del contrato cross-subsistema.

    ``PK = CAM#{site_id}#{device_id}#{camera_id}`` y
    ``SK = TS#{ts_event_ms:013d}#{event_id}`` (ver CLAUDE.md §8). Devuelve el dict
    low-level listo para ``Key=`` en GetItem/DeleteItem.
    """
    site_id = event["site_id"]
    device_id = event["device_id"]
    camera_id = event["camera_id"]
    ts_event_ms = int(event["ts_event_ms"])
    event_id = event["event_id"]
    pk = f"CAM#{site_id}#{device_id}#{camera_id}"
    sk = f"TS#{ts_event_ms:013d}#{event_id}"
    return {"PK": {"S": pk}, "SK": {"S": sk}}


def build_event_item(event: dict, *, clip_key: str | None = None) -> dict:
    """Construye el Item DynamoDB low-level de un ``CrossingEvent`` (contrato A).

    Incluye PK/SK + las claves del GSI1 por sitio (``GSI1PK=SITE#{site_id}``,
    ``GSI1SK=TS#{ts:013d}#{event_id}``) + los campos del contrato CrossingEvent
    (excepto ``synced``, que es SÓLO-LOCAL). ``clip_key`` (si se pasa) sobrescribe
    el del evento para reflejar la clave real del objeto ya subido a S3.
    """
    site_id = event["site_id"]
    ts_event_ms = int(event["ts_event_ms"])
    event_id = event["event_id"]
    ts_token = f"TS#{ts_event_ms:013d}#{event_id}"

    payload: dict[str, object] = {}
    for field_name in _CLOUD_EVENT_FIELDS:
        value = event.get(field_name)
        if value is not None:
            payload[field_name] = value
    if clip_key is not None:
        payload["clip_key"] = clip_key

    keys = event_keys(event)
    item = dict(keys)
    item["GSI1PK"] = {"S": f"SITE#{site_id}"}
    item["GSI1SK"] = {"S": ts_token}
    item.update(_item_to_dynamo(payload))
    return item


_CONTENT_TYPES = {"mp4": "video/mp4", "gif": "image/gif", "jpg": "image/jpeg"}


def _content_type_for(key: str) -> str:
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


class SyncOutcome:
    """Resultado lógico de sincronizar UN evento (valores de cadena estables)."""

    SYNCED_NEW = "synced_new"  # conditional-put aceptado: ítem nuevo en la nube
    SYNCED_DUPLICATE = "synced_duplicate"  # condicional rechazado: ya estaba (idempotente)


@dataclass
class SyncResult:
    """Estadísticas agregadas de una pasada ``sync_once`` (observabilidad/tests)."""

    synced_new: int = 0
    synced_duplicate: int = 0
    clips_uploaded: int = 0
    clips_already_present: int = 0
    deferred: int = 0  # eventos dejados en backlog por fallo transitorio (offline)
    errors: list[str] = field(default_factory=list)

    @property
    def synced_total(self) -> int:
        return self.synced_new + self.synced_duplicate


@dataclass
class CloudSyncConfig:
    """Configuración del worker de sync (nombres de recursos AWS + identidad del Pi).

    Los nombres de recurso por defecto coinciden con CLAUDE.md §4 / los outputs de
    PR04; en producción/integración se inyectan desde entorno o terraform output.
    """

    media_bucket: str
    events_table: str
    devices_table: str
    device_id: str
    app_version: str = "0.0.0"
    batch_size: int = 200


class CloudSync:
    """Worker de drenaje edge->cloud idempotente y tolerante a offline.

    Args:
        store: ``Store`` SQLite local (fuente única del trabajo pendiente).
        s3_client: cliente S3 inyectado (boto3 ``client('s3')`` o fake) con
            ``put_object``/``head_object``/``delete_object``.
        dynamodb_client: cliente DynamoDB inyectado (boto3 ``client('dynamodb')`` o
            fake) con ``put_item``/``get_item``/``update_item``/``delete_item``.
        config: ``CloudSyncConfig`` con nombres de recurso e identidad del device.
        clock: callable -> epoch segundos (inyectable en tests; por defecto
            ``time.monotonic``), usado SÓLO para el backoff, nunca para contratos.
    """

    def __init__(
        self,
        store: object,
        *,
        s3_client: object,
        dynamodb_client: object,
        config: CloudSyncConfig,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._s3 = s3_client
        self._ddb = dynamodb_client
        self._cfg = config
        self._clock = clock or time.monotonic
        # Estado del último upload de clip (para que sync_once contabilice; no es
        # contrato): "uploaded" | "already_present" | None.
        self._last_clip_upload_state: str | None = None

    # -- subida de clips a S3 (retry-safe) --------------------------------

    def _upload_clip(self, *, local_path: str, s3_key: str) -> str:
        """Sube un clip a S3 con ``If-None-Match: *`` (retry-safe, no pisa parcial).

        Returns:
            ``"uploaded"`` si subió el objeto; ``"already_present"`` si ya existía
            (412 PreconditionFailed: el primer escritor ganó; reintento idempotente).

        Lanza ``TransientSyncError`` (vía la clasificación del caller) ante fallos de
        red para que el evento quede en backlog.
        """
        path = Path(local_path)
        body = path.read_bytes()
        try:
            self._s3.put_object(  # type: ignore[attr-defined]
                Bucket=self._cfg.media_bucket,
                Key=s3_key,
                Body=body,
                ContentType=_content_type_for(s3_key),
                IfNoneMatch="*",
            )
            return "uploaded"
        except Exception as exc:  # noqa: BLE001 — clasificamos abajo
            if _is_precondition_failed(exc):
                # El objeto ya existe (subida previa del mismo event_id). No lo
                # pisamos: idempotente. NO es error.
                return "already_present"
            raise

    # -- conditional-put del evento en DynamoDB (idempotente) -------------

    def _put_event(self, event: dict, *, clip_key: str | None) -> str:
        """Conditional-put del CrossingEvent. Idempotente por ``event_id`` determinista.

        Returns:
            ``SyncOutcome.SYNCED_NEW`` si el ítem se creó; ``SYNCED_DUPLICATE`` si la
            condición ``attribute_not_exists(PK)`` lo rechazó (el evento ya estaba en
            la nube: éxito idempotente, NO error).
        """
        item = build_event_item(event, clip_key=clip_key)
        try:
            self._ddb.put_item(  # type: ignore[attr-defined]
                TableName=self._cfg.events_table,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            return SyncOutcome.SYNCED_NEW
        except Exception as exc:  # noqa: BLE001 — clasificamos abajo
            if _is_conditional_check_failed(exc):
                return SyncOutcome.SYNCED_DUPLICATE
            raise

    # -- sincronización de UN evento --------------------------------------

    def sync_event(self, event: dict) -> str:
        """Sincroniza UN evento: sube clip -> conditional-put -> marca ``synced=1``.

        Reutilizada tal cual por ``sync_once`` y por la prueba de integración real
        (que inyecta clientes boto3 reales). Devuelve el ``SyncOutcome``.

        Propaga ``TransientSyncError`` (offline/throttle) y errores duros (permisos,
        validación) para que el caller decida; en AMBOS casos NO marca ``synced``
        (el evento permanece en el backlog y no se pierde).
        """
        event_id = event["event_id"]
        clip_key = event.get("clip_key")

        clip_row = self._store.get_clip_upload_for_event(event_id)  # type: ignore[attr-defined]
        if clip_row is not None:
            s3_key = clip_row["s3_key_planned"]
            local_path = clip_row["local_path"]
            if clip_row.get("status") != "uploaded" and Path(local_path).exists():
                try:
                    upload_state = self._upload_clip(local_path=local_path, s3_key=s3_key)
                except Exception as exc:  # noqa: BLE001
                    if _is_transient(exc):
                        raise TransientSyncError(str(exc)) from exc
                    raise
                self._store.set_clip_upload_status(clip_row["id"], "uploaded")  # type: ignore[attr-defined]
                self._last_clip_upload_state = upload_state
            else:
                self._last_clip_upload_state = "already_present"
            clip_key = s3_key

        try:
            outcome = self._put_event(event, clip_key=clip_key)
        except Exception as exc:  # noqa: BLE001
            if _is_transient(exc):
                raise TransientSyncError(str(exc)) from exc
            raise

        # Reflejo local: la clave del clip real + estado, y el flag synced.
        if clip_key is not None:
            self._store.set_event_clip(event_id, clip_key, "uploaded")  # type: ignore[attr-defined]
        self._store.mark_event_synced(event_id)  # type: ignore[attr-defined]
        return outcome

    # -- drenaje del backlog ----------------------------------------------

    def sync_once(self) -> SyncResult:
        """Drena un lote de eventos ``synced=0``. NO lanza por fallo de red.

        Procesa en orden FIFO. Ante el PRIMER fallo transitorio (red caída) corta el
        lote y lo cuenta como ``deferred`` (backlog: se reintentará). Un evento
        duplicado (conditional rechazado) cuenta como sincronizado idempotente. Los
        errores duros se registran y cortan el lote (no se pierde el evento), pero NO
        tumban el proceso.
        """
        result = SyncResult()
        events = self._store.get_unsynced_events(limit=self._cfg.batch_size)  # type: ignore[attr-defined]
        for event in events:
            self._last_clip_upload_state = None
            try:
                outcome = self.sync_event(event)
            except TransientSyncError as exc:
                result.deferred += 1
                _log.info("sync diferido (offline/transitorio): %s", exc)
                break  # backlog: cortar el lote y reintentar más tarde
            except Exception as exc:  # noqa: BLE001 — error duro: registrar, no morir
                result.errors.append(f"{event.get('event_id')}: {exc}")
                _log.warning("sync: error duro en %s: %s", event.get("event_id"), exc)
                break
            if outcome == SyncOutcome.SYNCED_NEW:
                result.synced_new += 1
            else:
                result.synced_duplicate += 1
            if self._last_clip_upload_state == "uploaded":
                result.clips_uploaded += 1
            elif self._last_clip_upload_state == "already_present":
                result.clips_already_present += 1
        return result

    # -- heartbeat al registro de dispositivos (SOLO UpdateItem) ----------

    def heartbeat(
        self,
        *,
        status: str = "online",
        reported_version: str | None = None,
        last_seen_at: str | None = None,
    ) -> None:
        """Escribe un heartbeat al registro de dispositivos con SÓLO ``UpdateItem``.

        Actualiza ``reported_version`` (lo escribe el Pi), ``last_seen_at`` y
        ``status`` de la fila ``DEVICE#{device_id}``. **NUNCA hace GetItem/Query del
        registro**: el sync no lee el registro para decidir nada (manifest-no-registry).
        ``status`` se mapea con un alias porque es palabra reservada de DynamoDB.
        """
        version = reported_version if reported_version is not None else self._cfg.app_version
        seen = last_seen_at if last_seen_at is not None else _now_iso()
        self._ddb.update_item(  # type: ignore[attr-defined]
            TableName=self._cfg.devices_table,
            Key={"PK": {"S": f"DEVICE#{self._cfg.device_id}"}},
            UpdateExpression=(
                "SET reported_version = :v, last_seen_at = :t, #st = :s"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":v": {"S": version},
                ":t": {"S": seen},
                ":s": {"S": status},
            },
        )

    # -- bucle de servicio (offline-tolerante, backoff acotado) -----------

    def run_forever(
        self,
        *,
        stop_event: threading.Event,
        interval_s: float = 5.0,
        heartbeat_interval_s: float = 30.0,
        max_backoff_s: float = 60.0,
    ) -> None:
        """Bucle de drenaje + heartbeat hasta ``stop_event``. NO bloquea el conteo.

        Hace ``sync_once`` cada ``interval_s``; si una pasada deja backlog
        (``deferred``) o lanza, aplica backoff exponencial acotado a ``max_backoff_s``
        (la red está caída): el conteo sigue su curso en otro hilo/proceso y al
        reconectar el backlog drena sin duplicados. El heartbeat es best-effort
        (su fallo no detiene el drenaje).
        """
        backoff = interval_s
        last_heartbeat = 0.0
        while not stop_event.is_set():
            offline = False
            try:
                result = self.sync_once()
                offline = result.deferred > 0
            except Exception as exc:  # noqa: BLE001 — el worker NUNCA muere
                _log.warning("sync: pasada fallida: %s", exc)
                offline = True

            now = self._clock()
            if now - last_heartbeat >= heartbeat_interval_s:
                try:
                    self.heartbeat(status="online")
                    last_heartbeat = now
                except Exception as exc:  # noqa: BLE001 — heartbeat best-effort
                    _log.info("sync: heartbeat diferido: %s", exc)

            if offline:
                backoff = min(max_backoff_s, max(interval_s, backoff * 2))
            else:
                backoff = interval_s
            stop_event.wait(backoff)
