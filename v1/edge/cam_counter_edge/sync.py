"""Worker de sincronización edge -> cloud (tolerante a offline, manifest-no-registry).

Drena los ``CrossingEvent`` locales no sincronizados (``synced=0`` en SQLite) hacia
la nube de forma IDEMPOTENTE y desacoplada del camino de conteo:

1. **Subir el clip** del evento al bucket de media por SigV4 con el rol STS corto
   por-Pi. La subida es RETRY-SAFE: usa ``If-None-Match: *`` para NO pisar un
   upload parcial previo del MISMO ``event_id`` en un reintento (la
   ``s3_key_planned`` es estable porque ``event_id`` es DETERMINISTA). Si el objeto
   ya existe (``PreconditionFailed``), se considera subido (idempotente).
2. **Conditional-put del evento** en la tabla ``cam-counter-events`` con
   ``attribute_not_exists(PK)``. Un put rechazado por duplicado
   (``ConditionalCheckFailedException``) NO es error: el evento ya estaba en la
   nube, así que se marca ``synced=1`` igual (idempotencia del contrato A).
3. **Heartbeat al registro** de dispositivos SÓLO con ``UpdateItem``
   (``reported_version``/``last_seen_at``/``status``). El worker **NUNCA** lee el
   registro para decidir qué subir ni qué versión correr (la única fuente de la
   versión deseada es el manifiesto de canal en S3, fuera de este PR).

**Edge-first / offline-tolerante:** el conteo y la persistencia local NUNCA
dependen de la red. Cuando la red está caída, ``sync_once`` detiene el batch SIN
perder eventos (quedan ``synced=0``) y reintenta más tarde con backoff acotado; al
reconectar drena el backlog SIN duplicados (el mismo ``event_id`` deduplica en
DynamoDB por el conditional put y en S3 por la clave estable).

**Inyección de dependencias:** el acceso a AWS se hace por un par de clientes
(``s3``/``dynamodb``) estilo boto3 INYECTABLES (``AwsClients``). Así CI puede
stubear "red caída luego arriba" sin AWS real, y la prueba de integración real
inyecta clientes boto3 construidos desde credenciales STS del rol per-Pi. El
módulo NO importa boto3 a nivel de módulo (igual que el ``Detector`` con Hailo):
el factory por defecto lo importa de forma perezosa.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .identifiers import (
    validate_camera_id,
    validate_device_id,
    validate_site_id,
)
from .types import CrossingEvent

__all__ = [
    "IDEMPOTENT_CONDITION",
    "AwsClients",
    "CloudSync",
    "EventSyncOutcome",
    "SyncNetworkError",
    "SyncResult",
    "default_client_factory",
    "device_pk",
    "event_keys",
    "is_conditional_check_failed",
    "is_precondition_failed",
    "upload_event_clip",
]

# Condición idempotente del conditional-put, ALINEADA VERBATIM con la Lambda de
# ingesta (``lambdas/events_ingest/ddb.py``). El device y la Lambda DEBEN usar la
# MISMA condición sobre la MISMA (PK, SK): así, en dual-run (camino directo + MQTT),
# un mismo ``event_id`` con un ``ts_event_ms`` inmutable produce la MISMA SK y NO se
# duplica, lo decida quien lo decida. Si divergieran (p.ej. sólo PK), la SK podría
# diferir y el evento se duplicaría.
IDEMPOTENT_CONDITION = "attribute_not_exists(PK) AND attribute_not_exists(SK)"

_log = logging.getLogger(__name__)

# Nombres canónicos de recursos AWS (ver CLAUDE.md §7/§8). Son configuración
# PÚBLICA por convención de nombre, NO secretos. Se pueden override por entorno o
# por outputs de terraform; aquí sólo son DEFAULTS coherentes.
DEFAULT_MEDIA_BUCKET = "cam-counter-media-950639281773"
DEFAULT_EVENTS_TABLE = "cam-counter-events"
DEFAULT_DEVICES_TABLE = "cam-counter-devices"
DEFAULT_REGION = "us-east-1"

# Content-Types por extensión de clip (para el PutObject de media).
_CONTENT_TYPES = {
    "mp4": "video/mp4",
    "gif": "image/gif",
}


# --------------------------------------------------------------------------- #
# Detección de errores AWS sin importar botocore (igual filosofía que Hailo)
# --------------------------------------------------------------------------- #


def _error_code(exc: BaseException) -> str | None:
    """Extrae el ``Error.Code`` de una excepción estilo botocore ``ClientError``.

    botocore expone ``exc.response['Error']['Code']``. No importamos botocore para
    no acoplar el paquete de borde a AWS: leemos el atributo de forma defensiva, lo
    que funciona igual con un fake de test que imite ``.response``.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


def is_conditional_check_failed(exc: BaseException) -> bool:
    """``True`` si ``exc`` es un conditional put DynamoDB rechazado (duplicado)."""
    return _error_code(exc) == "ConditionalCheckFailedException"


def is_precondition_failed(exc: BaseException) -> bool:
    """``True`` si ``exc`` es un ``PutObject`` S3 rechazado por ``If-None-Match``."""
    return _error_code(exc) in {"PreconditionFailed", "PreconditionFailedException"}


class SyncNetworkError(RuntimeError):
    """Fallo de red/transitorio al hablar con AWS (la red está caída).

    El worker la usa para DETENER el batch sin perder eventos (quedan ``synced=0``)
    y reintentar más tarde. NO la lanzan los duplicados (esos son éxito idempotente).
    """


# --------------------------------------------------------------------------- #
# Clientes AWS inyectables
# --------------------------------------------------------------------------- #


class _S3Like(Protocol):
    """Subconjunto de la API boto3 S3 que usa el worker (put_object)."""

    def put_object(self, **kwargs: Any) -> Any: ...


class _DynamoLike(Protocol):
    """Subconjunto de la API boto3 DynamoDB que usa el worker."""

    def put_item(self, **kwargs: Any) -> Any: ...

    def update_item(self, **kwargs: Any) -> Any: ...


@dataclass
class AwsClients:
    """Par de clientes AWS (estilo boto3) que necesita el worker.

    Inyectable para tests (fakes) y para la prueba de integración real (clientes
    boto3 construidos con credenciales STS del rol per-Pi).
    """

    s3: _S3Like
    dynamodb: _DynamoLike


def default_client_factory(
    *,
    region: str = DEFAULT_REGION,
    role_arn: str | None = None,
    session_name: str = "cam-counter-edge-sync",
    duration_seconds: int = 3600,
) -> AwsClients:
    """Construye ``AwsClients`` reales con boto3 (import PEREZOSO).

    Si ``role_arn`` se da, asume el rol per-Pi vía STS y usa esas credenciales
    temporales (modelo de mínimo privilegio por dispositivo). Si no, usa las
    credenciales del entorno (cadena por defecto de boto3). NO se importa boto3 a
    nivel de módulo para que ``import cam_counter_edge.sync`` funcione sin boto3
    (CI puro de lógica usa fakes).
    """
    import boto3  # noqa: PLC0415  (import perezoso intencional, como Hailo)

    if role_arn:
        sts = boto3.client("sts", region_name=region)
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=int(duration_seconds),
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    else:
        session = boto3.Session(region_name=region)
    return AwsClients(
        s3=session.client("s3", region_name=region),
        dynamodb=session.client("dynamodb", region_name=region),
    )


# --------------------------------------------------------------------------- #
# Construcción de claves (slugs validados ANTES de construir cualquier clave)
# --------------------------------------------------------------------------- #


def event_keys(event: CrossingEvent) -> dict[str, str]:
    """Construye las claves DynamoDB del evento (PK/SK + GSI1) — slugs validados.

    - ``PK  = CAM#{site_id}#{device_id}#{camera_id}``
    - ``SK  = TS#{ts_event_ms:013d}#{event_id}``
    - ``GSI1PK = SITE#{site_id}``  /  ``GSI1SK = TS#{ts_event_ms:013d}#{event_id}``

    Valida los slugs ANTES de construir las claves para que ningún ``#``/``/`` se
    cuele (los slugs no admiten esos caracteres). El ``#`` de los delimitadores es
    intencional (clave compuesta DynamoDB).
    """
    validate_site_id(event.site_id)
    validate_device_id(event.device_id)
    validate_camera_id(event.camera_id)
    ts = f"{int(event.ts_event_ms):013d}"
    return {
        "PK": f"CAM#{event.site_id}#{event.device_id}#{event.camera_id}",
        "SK": f"TS#{ts}#{event.event_id}",
        "GSI1PK": f"SITE#{event.site_id}",
        "GSI1SK": f"TS#{ts}#{event.event_id}",
    }


def device_pk(device_id: str) -> str:
    """Clave de partición del registro de dispositivos: ``DEVICE#{device_id}``."""
    validate_device_id(device_id)
    return f"DEVICE#{device_id}"


def _ddb_event_item(event: CrossingEvent) -> dict[str, dict[str, str]]:
    """Serializa un ``CrossingEvent`` al formato de atributos DynamoDB (low-level).

    Construye el ítem en el formato tipado de la API de bajo nivel boto3
    (``{"S": ...}`` / ``{"N": ...}``), que es lo que acepta ``put_item``. Los
    campos opcionales ``None`` se OMITEN (no se escribe ``NULL`` para mantener el
    ítem compacto y el contrato limpio). ``synced`` es SÓLO-local: no se sube.
    """
    keys = event_keys(event)
    item: dict[str, dict[str, str]] = {
        "PK": {"S": keys["PK"]},
        "SK": {"S": keys["SK"]},
        "GSI1PK": {"S": keys["GSI1PK"]},
        "GSI1SK": {"S": keys["GSI1SK"]},
        "event_id": {"S": event.event_id},
        "site_id": {"S": event.site_id},
        "device_id": {"S": event.device_id},
        "camera_id": {"S": event.camera_id},
        "track_id": {"S": str(event.track_id)},
        "crossing_seq": {"N": str(int(event.crossing_seq))},
        "direction": {"S": event.direction},
        "ts_event_ms": {"N": str(int(event.ts_event_ms))},
        "ts_event_iso": {"S": event.ts_event_iso},
        "schema_version": {"N": str(int(event.schema_version))},
    }
    # Campos opcionales: sólo si tienen valor.
    if event.positive_label is not None:
        item["positive_label"] = {"S": event.positive_label}
    if event.negative_label is not None:
        item["negative_label"] = {"S": event.negative_label}
    if event.label is not None:
        item["label"] = {"S": event.label}
    if event.line_version is not None:
        item["line_version"] = {"N": str(int(event.line_version))}
    if event.confidence is not None:
        item["confidence"] = {"N": repr(float(event.confidence))}
    if event.clip_key is not None:
        item["clip_key"] = {"S": event.clip_key}
    if event.clip_status is not None:
        item["clip_status"] = {"S": event.clip_status}
    if event.created_at is not None:
        item["created_at"] = {"S": event.created_at}
    return item


# --------------------------------------------------------------------------- #
# Subida de clip a S3 (retry-safe) — compartida por el camino directo y MQTT
# --------------------------------------------------------------------------- #


def upload_event_clip(
    s3: _S3Like,
    store: Any,
    clip_row: dict,
    media_bucket: str,
) -> tuple[str | None, bool, bool]:
    """Sube el clip del evento a ``media_bucket`` con ``If-None-Match: *``, retry-safe.

    Idéntica idempotencia en el camino directo (``CloudSync``) y en el publicador
    MQTT (modo ``iot``, donde ``s3`` proviene de credenciales temporales del IoT
    Credential Provider): la ``s3_key_planned`` es estable porque el ``event_id`` es
    determinista, así que un reintento NO pisa un upload parcial previo.

    Returns:
        ``(clip_key, uploaded, already_present)``. ``clip_key`` es ``None`` si no hay
        clip local que subir (el evento se publica/sincroniza igual, sin media).
    """
    local_path = clip_row.get("local_path")
    s3_key = clip_row.get("s3_key_planned")
    if not local_path or not s3_key or not Path(local_path).is_file():
        return None, False, False

    row_id = int(clip_row["id"])
    store.set_clip_upload_status(row_id, "uploading", increment_attempts=True)
    ext = s3_key.rsplit(".", 1)[-1].lower()
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    body = Path(local_path).read_bytes()
    try:
        # If-None-Match: * => la PUT falla con PreconditionFailed si la clave ya
        # existe (no pisa un upload parcial/previo del mismo event_id).
        s3.put_object(
            Bucket=media_bucket,
            Key=s3_key,
            Body=body,
            ContentType=content_type,
            IfNoneMatch="*",
        )
        store.set_clip_upload_status(row_id, "uploaded")
        return s3_key, True, False
    except Exception as exc:  # noqa: BLE001 — clasificamos abajo
        if is_precondition_failed(exc):
            # El objeto ya estaba (reintento idempotente): clave válida.
            store.set_clip_upload_status(row_id, "uploaded")
            return s3_key, False, True
        store.set_clip_upload_status(row_id, "failed")
        raise


# --------------------------------------------------------------------------- #
# Resultados de la sincronización (observabilidad / aserciones de test)
# --------------------------------------------------------------------------- #


@dataclass
class EventSyncOutcome:
    """Resultado de sincronizar UN evento (para tests y observabilidad)."""

    event_id: str
    clip_uploaded: bool = False
    clip_already_present: bool = False
    put_new: bool = False
    put_duplicate: bool = False
    marked_synced: bool = False


@dataclass
class SyncResult:
    """Resultado agregado de un ``sync_once``."""

    processed: int = 0
    synced: int = 0
    stopped_offline: bool = False
    outcomes: list[EventSyncOutcome] = field(default_factory=list)


# Interfaz mínima del store que el worker necesita (para no acoplar a Store).
class _SyncStore(Protocol):
    def get_unsynced_events(self, limit: int = ...) -> list[CrossingEvent]: ...

    def get_clip_upload_for_event(self, event_id: str) -> dict | None: ...

    def set_clip_upload_status(
        self, row_id: int, status: str, *, increment_attempts: bool = ...
    ) -> None: ...

    def set_event_clip_key(
        self, event_id: str, clip_key: str, clip_status: str = ...
    ) -> None: ...

    def mark_event_synced(self, event_id: str) -> bool: ...


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


class CloudSync:
    """Worker de sincronización edge -> cloud (idempotente, offline-tolerante).

    Args:
        store: capa de persistencia local (expone ``get_unsynced_events``,
            ``mark_event_synced``, helpers de ``clip_uploads`` y ``clip_key``).
        device_id: device del Pi (para el heartbeat del registro).
        clients: clientes AWS inyectados; si ``None`` se construyen perezosamente
            con ``client_factory`` (boto3 real). Inyectar fakes en CI.
        client_factory: factory para construir ``AwsClients`` la primera vez.
        media_bucket/events_table/devices_table: nombres de recursos AWS.
        is_network_error: predicado que clasifica una excepción como "red caída"
            (transitoria). Por defecto, cualquier excepción que NO sea un duplicado
            idempotente se trata como transitoria (no se pierde el evento).
    """

    def __init__(
        self,
        store: _SyncStore,
        *,
        device_id: str,
        clients: AwsClients | None = None,
        client_factory: Callable[[], AwsClients] | None = None,
        media_bucket: str = DEFAULT_MEDIA_BUCKET,
        events_table: str = DEFAULT_EVENTS_TABLE,
        devices_table: str = DEFAULT_DEVICES_TABLE,
        is_network_error: Callable[[BaseException], bool] | None = None,
    ) -> None:
        self._store = store
        self._device_id = validate_device_id(device_id)
        self._clients = clients
        self._client_factory = client_factory
        self._media_bucket = media_bucket
        self._events_table = events_table
        self._devices_table = devices_table
        self._is_network_error = is_network_error

    # -- clientes ---------------------------------------------------------

    def _aws(self) -> AwsClients:
        """Devuelve los clientes AWS (construyéndolos perezosamente si hace falta)."""
        if self._clients is None:
            if self._client_factory is None:
                raise RuntimeError(
                    "CloudSync sin clientes ni client_factory: inyecta AwsClients "
                    "o un factory (p.ej. default_client_factory con role_arn)."
                )
            self._clients = self._client_factory()
        return self._clients

    def _classify(self, exc: BaseException) -> bool:
        """``True`` si ``exc`` es transitoria/red (debe detener el batch)."""
        if self._is_network_error is not None:
            return self._is_network_error(exc)
        # Por defecto: un duplicado idempotente NO es transitorio; cualquier otra
        # cosa se trata como transitoria para NO perder el evento (queda synced=0).
        return not (
            is_conditional_check_failed(exc) or is_precondition_failed(exc)
        )

    # -- subida de clip (retry-safe) -------------------------------------

    def _upload_clip(
        self, event: CrossingEvent, clip_row: dict
    ) -> tuple[str | None, bool, bool]:
        """Sube el clip del evento a S3 (``If-None-Match: *``), retry-safe.

        Delega en ``upload_event_clip`` (función de módulo) para que el camino
        directo y el publicador MQTT (modo ``iot``) compartan EXACTAMENTE la misma
        subida idempotente de clips.
        """
        return upload_event_clip(
            self._aws().s3, self._store, clip_row, self._media_bucket
        )

    # -- conditional put del evento --------------------------------------

    def _put_event(self, event: CrossingEvent) -> bool:
        """Conditional-put del evento. Devuelve ``True`` si creó un ítem NUEVO.

        Un ``ConditionalCheckFailedException`` (duplicado) NO es error: devuelve
        ``False`` (el evento ya estaba en la nube; idempotencia del contrato A).
        Otra excepción se propaga (la clasifica el caller como transitoria).
        """
        item = _ddb_event_item(event)
        try:
            self._aws().dynamodb.put_item(
                TableName=self._events_table,
                Item=item,
                ConditionExpression=IDEMPOTENT_CONDITION,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if is_conditional_check_failed(exc):
                return False  # duplicado: éxito idempotente
            raise

    # -- sincronización de un evento -------------------------------------

    def sync_event(self, event: CrossingEvent) -> EventSyncOutcome:
        """Sincroniza UN evento: subir clip -> conditional-put -> marcar synced=1.

        Reutilizada por el bucle ``sync_once`` y por la prueba de integración real
        (que inyecta un cliente boto3 real). Lanza la excepción transitoria si la
        red está caída (el evento queda ``synced=0`` para reintentar).
        """
        outcome = EventSyncOutcome(event_id=event.event_id)
        clip_row = self._store.get_clip_upload_for_event(event.event_id)
        if clip_row is not None:
            key, uploaded, already = self._upload_clip(event, clip_row)
            if key is not None:
                outcome.clip_uploaded = uploaded
                outcome.clip_already_present = already
                # Refleja la clave en el evento local antes del put (clip_status).
                self._store.set_event_clip_key(event.event_id, key, "uploaded")
                event.clip_key = key
                event.clip_status = "uploaded"

        created = self._put_event(event)
        outcome.put_new = created
        outcome.put_duplicate = not created
        outcome.marked_synced = self._store.mark_event_synced(event.event_id)
        return outcome

    def sync_once(self, *, limit: int = 100) -> SyncResult:
        """Drena un batch de eventos ``synced=0``. Best-effort y offline-tolerant.

        Procesa en orden los pendientes; ante una excepción TRANSITORIA (red
        caída) DETIENE el batch (sin perder eventos: quedan ``synced=0``) y marca
        ``stopped_offline``. Los duplicados idempotentes cuentan como sincronizados.
        """
        result = SyncResult()
        events = self._store.get_unsynced_events(limit)
        result.processed = len(events)
        for event in events:
            try:
                outcome = self.sync_event(event)
            except Exception as exc:  # noqa: BLE001
                if self._classify(exc):
                    # Red caída: paramos el batch; el evento sigue synced=0.
                    _log.warning(
                        "cloud-sync: fallo transitorio en %s (%r); se reintentará",
                        event.event_id,
                        exc,
                    )
                    result.stopped_offline = True
                    break
                raise
            result.outcomes.append(outcome)
            if outcome.marked_synced:
                result.synced += 1
        return result

    # -- heartbeat al registro (SÓLO UpdateItem; NUNCA lee el registro) --

    def heartbeat(
        self,
        *,
        reported_version: str,
        status: str = "online",
        last_seen_at: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        """Escribe heartbeat al registro de dispositivos SÓLO con ``UpdateItem``.

        Actualiza ``reported_version``/``last_seen_at``/``status`` (y opcionales
        en ``extra``) de la propia fila ``DEVICE#{device_id}``. **NUNCA** lee el
        registro: la decisión de qué subir o qué versión correr NO depende del
        registro (manifest-no-registry). Tolerante a offline: si la red está caída
        lanza la transitoria (best-effort; no bloquea el conteo).
        """
        if last_seen_at is None:
            last_seen_at = _now_iso()
        names = {"#rv": "reported_version", "#ls": "last_seen_at", "#st": "status"}
        values = {
            ":rv": {"S": reported_version},
            ":ls": {"S": last_seen_at},
            ":st": {"S": status},
        }
        sets = ["#rv = :rv", "#ls = :ls", "#st = :st"]
        if extra:
            for i, (k, v) in enumerate(extra.items()):
                names[f"#x{i}"] = k
                values[f":x{i}"] = {"S": v}
                sets.append(f"#x{i} = :x{i}")
        self._aws().dynamodb.update_item(
            TableName=self._devices_table,
            Key={"PK": {"S": device_pk(self._device_id)}},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )


def _now_iso() -> str:
    """Timestamp ISO-8601 UTC (segundos) de pared para el heartbeat (no contrato)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
