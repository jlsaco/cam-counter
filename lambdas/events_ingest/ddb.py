"""Helpers DynamoDB de la ingesta: conditional put idempotente + avance de clip_status.

El núcleo de la idempotencia es ``CONDITION_NEW_ITEM``:
``attribute_not_exists(PK) AND attribute_not_exists(SK)`` — EXACTAMENTE la misma
condición que usa el camino directo del borde (``cam_counter_edge.sync``). Reintentar
el MISMO ``event_id`` produce idéntica PK/SK ⇒ el segundo put se rechaza con
``ConditionalCheckFailedException``, que el handler trata como ÉXITO (dup), no error.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

# Condición de "ítem nuevo" — UNIFICADA con el borde (WP05, nota del revisor).
CONDITION_NEW_ITEM = "attribute_not_exists(PK) AND attribute_not_exists(SK)"


def is_conditional_check_failed(exc: BaseException) -> bool:
    """True si la excepción es un ``ConditionalCheckFailedException`` de DynamoDB."""
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _to_dynamo(value: Any) -> Any:
    """Convierte floats a ``Decimal`` (boto3 resource no acepta float en items)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        # Pasa por str para evitar la imprecisión binaria del float directo.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def conditional_put_event(table: Any, item: dict) -> None:
    """Conditional put idempotente. Lanza ``ClientError`` (cond-failed) si ya existía.

    Los ``None`` se OMITEN (no se escribe NULL: ítem compacto, contrato limpio).
    """
    clean = {k: _to_dynamo(v) for k, v in item.items() if v is not None}
    table.put_item(Item=clean, ConditionExpression=CONDITION_NEW_ITEM)


def maybe_advance_clip_status(table: Any, item: dict) -> bool:
    """Avanza ``clip_status`` ``pending``→``uploaded`` de forma MONÓTONA (nunca retrocede).

    Sólo actúa si el evento entrante trae ``clip_status == "uploaded"`` (p. ej. el
    re-publish llega tras subirse el clip). El ``UpdateItem`` es condicional a que el
    ítem siga en ``pending``; cualquier otro estado deja el valor intacto. Devuelve
    True si avanzó, False en caso contrario.
    """
    if item.get("clip_status") != "uploaded":
        return False
    try:
        table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET clip_status = :u",
            ConditionExpression="clip_status = :p",
            ExpressionAttributeValues={":u": "uploaded", ":p": "pending"},
        )
        return True
    except ClientError as exc:
        if is_conditional_check_failed(exc):
            return False  # ya no estaba en pending: no-op idempotente
        raise
