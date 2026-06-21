"""Worker ``cloud-sync``: drena eventos locales no sincronizados a la nube.

Cierra el lazo edge->cloud del producto SIN romper la filosofía edge-first: el
conteo y la persistencia LOCAL (SQLite) NUNCA dependen de la red; este worker es
best-effort, DESACOPLADO del conteo, IDEMPOTENTE y TOLERANTE A OFFLINE.

Bucle de drenaje (``drain_once``), por cada evento ``synced=0`` (más viejo
primero):

1. **Sube el clip** al bucket de media por SigV4 (cliente boto3 con credenciales
   STS cortas del rol per-Pi). RETRY-SAFE: usa ``If-None-Match: *`` para NO pisar
   una subida previa del MISMO ``event_id`` en un reintento (si el objeto ya
   existe, S3 responde ``PreconditionFailed`` y lo tratamos como subida ya hecha).
2. **Conditional put del ``CrossingEvent``** en la tabla de eventos con
   ``attribute_not_exists(PK)``. IDEMPOTENTE vía el ``event_id`` DETERMINISTA: un
   put rechazado por duplicado (``ConditionalCheckFailedException``) NO es error;
   se marca ``synced=1`` igual (el evento ya está en la nube).
3. **Marca ``synced=1``** en SQLite sólo tras éxito (o duplicado idempotente).

Heartbeat (``heartbeat``): escribe al registro de dispositivos SÓLO con
``UpdateItem`` (``reported_version``, ``last_seen_at``, ``status``). El worker
**NUNCA lee el registro** para decidir qué subir ni qué versión correr: la fuente
única de la versión deseada es el manifiesto de canal en S3 (fuera de alcance
aquí). Mantener intacta esa regla *manifest-no-registry* es un invariante.

Offline explícito: cuando la red está caída, una llamada AWS lanza un error de
red; ``drain_once`` lo detecta, ABORTA el pase (sin marcar el evento) y lo deja
para el siguiente intento. Los eventos quedan ``synced=0`` y se reenvían al
reconectar — sin duplicar, porque el ``event_id`` determinista deduplica en S3 y
en DynamoDB. El conteo, mientras tanto, sigue contando y persistiendo en local.

Inyección de dependencias: los clientes AWS (S3 + DynamoDB) se PASAN al worker
(no se construyen dentro). Así CI los stubea ("red caída luego arriba") sin AWS
real, y la prueba de integración inyecta clientes boto3 reales construidos a
partir de credenciales STS del rol per-Pi. ``sync.py`` NO importa boto3 a nivel
de módulo: la detección de errores AWS es por *duck-typing* del atributo
``response`` (mismo shape que ``botocore.exceptions.ClientError``).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .identifiers import validate_camera_id, validate_device_id, validate_site_id
from .types import CrossingEvent

__all__ = [
    "AwsClients",
    "CloudSyncWorker",
    "DrainResult",
    "SyncConfig",
    "SyncOfflineError",
    "build_boto3_clients",
    "media_key_for",
]

_log = logging.getLogger("cam_counter_edge.sync")

# Defaults coherentes con CLAUDE.md §7 y la spec §4 (recursos reales de PR04).
DEFAULT_MEDIA_BUCKET = "cam-counter-media-950639281773"
DEFAULT_EVENTS_TABLE = "cam-counter-events"
DEFAULT_DEVICES_TABLE = "cam-counter-devices"

# Códigos de error AWS que tratamos como "ya hecho" (idempotente, no fallo).
_CLIP_ALREADY_EXISTS_CODES = frozenset({"PreconditionFailed"})
_PUT_DUPLICATE_CODES = frozenset({"ConditionalCheckFailedException"})

# Errores transitorios / de red => OFFLINE: abortamos el pase y reintentamos.
_OFFLINE_EXC_NAMES = frozenset(
    {
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "ConnectionError",
        "ProxyConnectionError",
        "HTTPClientError",
    }
)
_OFFLINE_AWS_CODES = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "ThrottlingError",
        "RequestLimitExceeded",
        "InternalServerError",
        "ServiceUnavailable",
        "RequestTimeout",
        "RequestTimeoutException",
        "TransactionInProgressException",
        "SlowDown",
    }
)

# Campos del CrossingEvent que SÍ viajan a la nube. ``synced`` es SÓLO-LOCAL
# (ver contracts/crossing_event.schema.json) y NUNCA se persiste en DynamoDB.
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


class _Store(Protocol):
    """Forma mínima del store que el worker necesita (sólo lo LOCAL)."""

    def get_unsynced_events(self, limit: int = ...) -> list[CrossingEvent]: ...
    def mark_synced(self, event_id: str) -> None: ...


class _ClipLoader(Protocol):
    """Resuelve los BYTES del clip local de un evento, o ``None`` si no hay."""

    def __call__(self, event: CrossingEvent) -> bytes | None: ...


@dataclass
class AwsClients:
    """Clientes AWS inyectables que el worker usa (S3 + DynamoDB).

    Se PASAN ya construidos para desacoplar la lógica del transporte: CI inyecta
    fakes ("red caída luego arriba"); la integración real inyecta clientes boto3
    construidos con credenciales STS del rol per-Pi (ver ``build_boto3_clients``).
    """

    s3: Any
    dynamodb: Any


@dataclass
class DrainResult:
    """Resumen de un pase de ``drain_once`` (observabilidad y aserciones de test).

    Attributes:
        scanned: eventos ``synced=0`` examinados en el pase.
        written: ``CrossingEvent`` escritos NUEVOS en DynamoDB (put aceptado).
        duplicates: puts rechazados por duplicado (idempotentes; no son fallo).
        clips_uploaded: clips subidos a S3 en este pase.
        synced: eventos marcados ``synced=1`` (nuevos + duplicados idempotentes).
        offline: ``True`` si el pase se cortó por un error de red/transitorio.
    """

    scanned: int = 0
    written: int = 0
    duplicates: int = 0
    clips_uploaded: int = 0
    synced: int = 0
    offline: bool = False


@dataclass
class SyncConfig:
    """Configuración del worker (nombres de recursos + identidad del Pi)."""

    device_id: str
    media_bucket: str = DEFAULT_MEDIA_BUCKET
    events_table: str = DEFAULT_EVENTS_TABLE
    devices_table: str = DEFAULT_DEVICES_TABLE
    batch_limit: int = 100
    base_backoff_s: float = 1.0
    max_backoff_s: float = 60.0
    idle_interval_s: float = 5.0

    def __post_init__(self) -> None:
        validate_device_id(self.device_id)


class SyncOfflineError(RuntimeError):
    """La red está caída (o un error transitorio): abortar el pase y reintentar.

    NO es un fallo del producto: el conteo sigue en local y el backlog se drena al
    reconectar. La lleva el worker internamente para cortar un ``drain_once``.
    """


def _now_iso() -> str:
    """ISO-8601 UTC (precisión de ms) del instante actual."""
    return (
        datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _error_code(exc: BaseException) -> str | None:
    """Código de error AWS (``response['Error']['Code']``) por duck-typing.

    Evita importar botocore: cualquier excepción con el mismo *shape* que
    ``ClientError`` (un dict ``response`` con ``Error.Code``) encaja. Los fakes de
    CI reproducen ese shape para simular AWS sin red.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


def _is_offline_error(exc: BaseException, code: str | None) -> bool:
    """¿Es un error de red/transitorio que justifica reintentar (offline)?"""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if type(exc).__name__ in _OFFLINE_EXC_NAMES:
        return True
    return code in _OFFLINE_AWS_CODES


def media_key_for(event: CrossingEvent, ext: str = "mp4") -> str:
    """Clave S3 canónica del media del evento (valida slugs ANTES de construirla).

    Convención (CLAUDE.md §7):
    ``media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}``.
    La fecha sale de ``ts_event_ms`` en UTC. Los slugs se validan para garantizar
    que la clave no contenga ``#`` ni ``/`` fuera de los separadores de ruta.
    """
    validate_site_id(event.site_id)
    validate_device_id(event.device_id)
    validate_camera_id(event.camera_id)
    day = datetime.fromtimestamp(event.ts_event_ms / 1000.0, tz=UTC)
    return (
        f"media/{event.site_id}/{event.device_id}/{event.camera_id}/"
        f"{day:%Y}/{day:%m}/{day:%d}/{event.event_id}.{ext}"
    )


def _attr(value: object) -> dict[str, Any] | None:
    """Marshalla un valor Python al formato de atributo low-level de DynamoDB.

    Devuelve ``None`` para valores ``None`` (el caller los OMITE del item). El
    orden de los ``isinstance`` importa: ``bool`` es subclase de ``int``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, float):
        return {"N": repr(value)}
    if isinstance(value, str):
        return {"S": value}
    raise TypeError(f"tipo no marshallable a DynamoDB: {type(value)!r}")


def event_pk(event: CrossingEvent) -> str:
    """Partition key del evento en DynamoDB: ``CAM#{site}#{device}#{camera}``."""
    return f"CAM#{event.site_id}#{event.device_id}#{event.camera_id}"


def event_sk(event: CrossingEvent) -> str:
    """Sort key del evento: ``TS#{ts_event_ms:013d}#{event_id}`` (orden temporal)."""
    return f"TS#{event.ts_event_ms:013d}#{event.event_id}"


def build_boto3_clients(
    *,
    region_name: str = "us-east-1",
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
) -> AwsClients:
    """Construye ``AwsClients`` boto3 reales (import PEREZOSO de boto3).

    Úsalo en el Pi y en la prueba de integración real. Si se pasan credenciales
    (p.ej. las STS temporales de ``sts:AssumeRole`` del rol per-Pi), se usan; si
    no, boto3 resuelve la cadena estándar (entorno/perfil/instancia).
    """
    import boto3  # noqa: PLC0415  (perezoso: CI con fakes no necesita boto3)

    session = boto3.session.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        aws_session_token=aws_session_token,
        region_name=region_name,
    )
    return AwsClients(s3=session.client("s3"), dynamodb=session.client("dynamodb"))


class CloudSyncWorker:
    """Drena eventos ``synced=0`` a la nube de forma idempotente y offline-tolerante.

    Args:
        store: store SQLite local (expone ``get_unsynced_events``/``mark_synced``).
        clients: clientes AWS inyectados (S3 + DynamoDB).
        config: nombres de recursos + identidad del Pi.
        clip_loader: callable ``event -> bytes | None`` que resuelve el clip local
            del evento. Por defecto no hay clip (``None``): el evento se sincroniza
            igual con ``clip_key=None``. La integración real inyecta uno que
            devuelve un MP4 de prueba; en el Pi lo provee la cola de ``clip.py``.
    """

    def __init__(
        self,
        store: _Store,
        clients: AwsClients,
        config: SyncConfig,
        clip_loader: _ClipLoader | None = None,
    ) -> None:
        self._store = store
        self._clients = clients
        self._cfg = config
        self._clip_loader = clip_loader
        self._stop = threading.Event()

    # -- subida de clip ---------------------------------------------------

    def _upload_clip(self, event: CrossingEvent) -> str | None:
        """Sube el clip local (si lo hay) RETRY-SAFE y devuelve su clave S3.

        Usa ``If-None-Match: *`` para no pisar una subida previa del mismo
        ``event_id``: si el objeto ya existe, S3 responde ``PreconditionFailed`` y
        lo tratamos como subida ya completada (idempotente). Devuelve ``None`` si
        el evento no tiene clip local que subir.
        """
        load = self._clip_loader
        clip = load(event) if load is not None else None
        if clip is None:
            return None
        key = media_key_for(event)
        try:
            self._clients.s3.put_object(
                Bucket=self._cfg.media_bucket,
                Key=key,
                Body=clip,
                IfNoneMatch="*",
                ContentType="video/mp4",
            )
        except Exception as exc:  # noqa: BLE001 (clasificamos por código AWS)
            code = _error_code(exc)
            if code in _CLIP_ALREADY_EXISTS_CODES:
                _log.info("clip ya existía (idempotente): %s", key)
                return key
            if _is_offline_error(exc, code):
                raise SyncOfflineError(f"red caída subiendo clip {key}") from exc
            raise
        return key

    # -- conditional put del evento --------------------------------------

    def _event_item(self, event: CrossingEvent, clip_key: str | None) -> dict[str, Any]:
        """Construye el item DynamoDB del evento (claves + GSI1 por sitio).

        ``clip_key``/``clip_status`` reflejan el resultado de la subida; ``synced``
        NO se incluye (es SÓLO-LOCAL).
        """
        item: dict[str, Any] = {
            "PK": {"S": event_pk(event)},
            "SK": {"S": event_sk(event)},
            "GSI1PK": {"S": f"SITE#{event.site_id}"},
            "GSI1SK": {"S": event_sk(event)},
        }
        overrides = {
            "clip_key": clip_key,
            "clip_status": "uploaded" if clip_key else event.clip_status,
        }
        for field_name in _CLOUD_EVENT_FIELDS:
            value = overrides.get(field_name, getattr(event, field_name))
            attr = _attr(value)
            if attr is not None:  # omitimos los campos opcionales nulos
                item[field_name] = attr
        return item

    def _put_event(self, event: CrossingEvent, clip_key: str | None) -> bool:
        """Conditional put del evento. ``True`` si fue NUEVO; ``False`` si duplicado.

        ``attribute_not_exists(PK)`` hace el put IDEMPOTENTE: un reintento del
        mismo ``event_id`` (misma PK/SK determinista) es rechazado con
        ``ConditionalCheckFailedException`` y NO crea un segundo item.
        """
        try:
            self._clients.dynamodb.put_item(
                TableName=self._cfg.events_table,
                Item=self._event_item(event, clip_key),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as exc:  # noqa: BLE001 (clasificamos por código AWS)
            code = _error_code(exc)
            if code in _PUT_DUPLICATE_CODES:
                _log.info("evento duplicado (idempotente): %s", event.event_id)
                return False
            if _is_offline_error(exc, code):
                raise SyncOfflineError(
                    f"red caída escribiendo evento {event.event_id}"
                ) from exc
            raise
        return True

    # -- drenaje ----------------------------------------------------------

    def drain_once(self) -> DrainResult:
        """Un pase de drenaje del backlog local ``synced=0``.

        Procesa los eventos del más viejo al más nuevo. Ante un error de red corta
        el pase (``offline=True``) SIN marcar el evento en curso, dejándolo para el
        siguiente intento (no se pierde ni se duplica). Un duplicado idempotente NO
        corta el pase: marca ``synced=1`` y continúa.
        """
        result = DrainResult()
        events = self._store.get_unsynced_events(limit=self._cfg.batch_limit)
        for event in events:
            result.scanned += 1
            try:
                clip_key = self._upload_clip(event)
                if clip_key is not None:
                    result.clips_uploaded += 1
                written = self._put_event(event, clip_key)
            except SyncOfflineError as exc:
                _log.warning("sync offline; backlog conservado: %s", exc)
                result.offline = True
                break
            if written:
                result.written += 1
            else:
                result.duplicates += 1
            self._store.mark_synced(event.event_id)
            result.synced += 1
        return result

    def heartbeat(
        self,
        *,
        reported_version: str,
        status: str = "online",
        last_seen_at: str | None = None,
    ) -> None:
        """Escribe el heartbeat del dispositivo al registro SÓLO con ``UpdateItem``.

        Actualiza ``reported_version``, ``last_seen_at`` y ``status`` de la fila
        ``DEVICE#{device_id}``. El worker **NUNCA** lee el registro: esta es la
        ÚNICA vía por la que toca la tabla de dispositivos (regla
        *manifest-no-registry*; la versión deseada vive en el manifiesto de canal).
        """
        ts = last_seen_at or _now_iso()
        self._clients.dynamodb.update_item(
            TableName=self._cfg.devices_table,
            Key={"PK": {"S": f"DEVICE#{self._cfg.device_id}"}},
            UpdateExpression=(
                "SET reported_version = :v, last_seen_at = :t, #s = :st"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":v": {"S": reported_version},
                ":t": {"S": ts},
                ":st": {"S": status},
            },
        )

    # -- bucle de servicio ------------------------------------------------

    def stop(self) -> None:
        """Señala al bucle ``run`` que pare tras el pase en curso."""
        self._stop.set()

    def run(
        self,
        *,
        sleep: Any = None,
        max_passes: int | None = None,
    ) -> None:
        """Bucle de servicio: drena con BACKOFF acotado mientras no se pare.

        Tras un pase OFFLINE espera con backoff exponencial acotado
        (``base_backoff_s``..``max_backoff_s``); tras un pase ok el backoff se
        resetea y, si no había trabajo, espera ``idle_interval_s``. ``sleep`` es
        inyectable para tests; ``max_passes`` acota el número de pases (tests).
        """
        if sleep is None:
            import time  # noqa: PLC0415  (sólo el servicio real necesita time)

            sleep = time.sleep
        backoff = self._cfg.base_backoff_s
        passes = 0
        while not self._stop.is_set():
            if max_passes is not None and passes >= max_passes:
                break
            result = self.drain_once()
            passes += 1
            if result.offline:
                sleep(backoff)
                backoff = min(backoff * 2.0, self._cfg.max_backoff_s)
            else:
                backoff = self._cfg.base_backoff_s
                if result.scanned == 0:
                    sleep(self._cfg.idle_interval_s)
