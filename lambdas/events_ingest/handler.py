"""Lambda ``cam-counter-events-ingest`` — ingesta idempotente de CrossingEvents.

Destino de la IoT Rule de cruces (WP06): ``cam-counter/{device_id}/events/crossing``
→ esta Lambda → DynamoDB ``cam-counter-events``. Pasos por evento:

1. **Validación VERBATIM** del payload contra el contrato ``crossing_event``
   (mismo schema que el gate de WP02), fail-closed.
2. **Anti-spoof**: ``event_id`` recomputado DETERMINISTA debe coincidir con el del
   payload; ``clip_key`` (si viene) acotado a la identidad del propio evento.
3. **Conditional put** ``attribute_not_exists(PK) AND attribute_not_exists(SK)``:
   idempotente (un reintento o el dual-write directo+MQTT NO duplica). El
   ``ConditionalCheckFailedException`` es ÉXITO.
4. **Enlace de clip** (``_maybe_link_clip``): si el evento ya existía pero el
   payload trae media, rellena ``clip_key`` sin pisar uno previo.
5. Marca ``_ingest_ts_ms`` (SÓLO este camino) → paridad medida EN LA INGESTA.

Autocontenida: stdlib + ``boto3`` del runtime (import perezoso). Schema HORNEADO
al lado del handler (lo copia Terraform/``archive_file`` y ``make build-lambdas``).
La validación NO duplica datos sensibles ni secretos; sólo config pública.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from contract import ContractError, load_schema, validate_crossing_event
from ddb import (
    DynamoLike,
    default_dynamodb_client,
    put_event_idempotent,
    serialize_event_item,
    try_link_clip,
)
from keys import build_keys, recompute_event_id, validate_clip_key

__all__ = ["handler", "process_event"]

_log = logging.getLogger()
_log.setLevel(logging.INFO)

# Config pública por entorno (canon CAMCOUNTER_*); defaults = nombres reales.
_EVENTS_TABLE = os.environ.get("CAMCOUNTER_EVENTS_TABLE", "cam-counter-events")
_REGION = os.environ.get("CAMCOUNTER_REGION", "us-east-1")

# Schema cacheado en frío (una carga por contenedor). El payload se valida verbatim.
_SCHEMA: dict | None = None

# Cliente DynamoDB cacheado (lazy). Inyectable en tests vía ``process_event``.
_CLIENT: DynamoLike | None = None


def _schema() -> dict:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = load_schema()
    return _SCHEMA


def _client() -> DynamoLike:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = default_dynamodb_client(_REGION)
    return _CLIENT


class SpoofError(ValueError):
    """El payload es contractualmente válido pero falla un control anti-spoof."""


def _now_ms() -> int:
    """Epoch ms del momento de ingesta (marca ``_ingest_ts_ms``)."""
    return int(time.time() * 1000)


def _anti_spoof(event: dict) -> None:
    """Controles anti-spoof tras la validación de contrato.

    - ``event_id`` recomputado DETERMINISTA == el del payload (un device no puede
      reclamar un id arbitrario; preserva la dedupe idempotente).
    - ``clip_key`` (si viene) acotado a la identidad del propio evento.
    """
    expected = recompute_event_id(
        event["site_id"],
        event["device_id"],
        event["camera_id"],
        event["track_id"],
        event["crossing_seq"],
    )
    if event["event_id"] != expected:
        raise SpoofError(
            f"event_id no determinista: payload {event['event_id']!r} != recomputado {expected!r}"
        )

    clip_key = event.get("clip_key")
    if clip_key is not None:
        validate_clip_key(clip_key, event)


def process_event(
    event: dict,
    *,
    table_name: str = _EVENTS_TABLE,
    client: DynamoLike | None = None,
    schema: dict | None = None,
    now_ms: Callable[[], int] = _now_ms,
) -> dict[str, Any]:
    """Procesa UN CrossingEvent (validar → anti-spoof → put idempotente → clip).

    Devuelve un dict de resultado (para logs/tests). Inyectable: ``client`` y
    ``schema`` permiten tests sin AWS ni schema horneado.
    """
    validate_crossing_event(event, schema if schema is not None else _schema())
    _anti_spoof(event)

    ddb = client if client is not None else _client()
    keys = build_keys(event)
    ingest_ts_ms = now_ms()
    item = serialize_event_item(event, ingest_ts_ms)

    created = put_event_idempotent(ddb, table_name, item)
    clip_linked = False
    if not created and event.get("clip_key") is not None:
        # Duplicado pero con media: enlaza el clip si el item no lo tenía.
        clip_linked = try_link_clip(
            ddb,
            table_name,
            keys,
            event["clip_key"],
            event.get("clip_status", "uploaded"),
        )

    result = {
        "event_id": event["event_id"],
        "pk": keys["PK"],
        "sk": keys["SK"],
        "created": created,
        "duplicate": not created,
        "clip_linked": clip_linked,
        "ingest_ts_ms": ingest_ts_ms,
    }
    _log.info("events-ingest %s", result)
    return result


def _extract_payloads(event: Any) -> list[dict]:
    """Normaliza la invocación a una lista de payloads CrossingEvent.

    La IoT Rule (WP06) invoca con el JSON del mensaje MQTT como payload directo
    (un único evento). Se acepta además una lista (batch) por robustez. NO se
    asume formato SQS/Kinesis: la Rule entrega el cuerpo MQTT tal cual.
    """
    if isinstance(event, list):
        return [e for e in event if isinstance(e, dict)]
    if isinstance(event, dict):
        return [event]
    return []


def handler(event: Any, context: Any = None) -> dict[str, Any]:  # noqa: ARG001
    """Entry-point Lambda (``handler.handler``).

    Un payload contractualmente inválido o spoofeado se RECHAZA (se loguea y se
    cuenta), NO se reintenta: reintentarlo nunca lo haría válido y sólo llenaría la
    DLQ. Los fallos transitorios de DynamoDB SÍ se propagan (→ reintentos async →
    DLQ ``cam-counter-ingest-dlq``).
    """
    payloads = _extract_payloads(event)
    processed = 0
    created = 0
    duplicates = 0
    rejected = 0

    for payload in payloads:
        try:
            outcome = process_event(payload)
        except (ContractError, SpoofError, ValueError) as exc:
            # Rechazo DETERMINISTA: no reintentar (no lo arregla un retry).
            rejected += 1
            _log.warning("events-ingest RECHAZADO (no se reintenta): %s", exc)
            continue
        processed += 1
        created += 1 if outcome["created"] else 0
        duplicates += 1 if outcome["duplicate"] else 0

    summary = {
        "received": len(payloads),
        "processed": processed,
        "created": created,
        "duplicates": duplicates,
        "rejected": rejected,
    }
    _log.info("events-ingest summary %s", summary)
    return summary
