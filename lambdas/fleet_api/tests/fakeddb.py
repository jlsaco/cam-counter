"""Tabla DynamoDB en memoria mínima para los tests de `fleet_api` (sin AWS real).

Implementa SÓLO lo que la capa `ddb.py` usa: ``query`` (con ``IndexName``,
``KeyConditionExpression`` de igualdad sobre la PK/GSI1PK, ``Limit``, ``ExclusiveStartKey``,
``ScanIndexForward``) y ``get_item``. Reproduce fielmente la paginación de DynamoDB
(``LastEvaluatedKey`` cuando hay más resultados) para poder ejercitar el cursor opaco y la
fusión de canales sin servicio real.
"""

from __future__ import annotations

from typing import Any

# Atributos de clave por tipo de consulta (base vs. índice GSI1).
_BASE_KEYS = ("PK", "SK")
_GSI1_KEYS = ("PK", "SK", "GSI1PK", "GSI1SK")


def _eq_target(condition: Any) -> tuple[str, Any]:
    """Extrae (atributo, valor) de un ``Key(attr).eq(value)`` de boto3."""
    expr = condition.get_expression()
    attr, value = expr["values"]
    return attr.name, value


class FakeTable:
    def __init__(self, items: list[dict[str, Any]]):
        self.items = [dict(i) for i in items]

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:  # noqa: N803 (boto3 API)
        for item in self.items:
            if all(item.get(k) == v for k, v in Key.items()):
                return {"Item": dict(item)}
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        index = kwargs.get("IndexName")
        pk_attr, pk_value = _eq_target(kwargs["KeyConditionExpression"])
        sort_attr = "GSI1SK" if index else "SK"
        key_attrs = _GSI1_KEYS if index else _BASE_KEYS

        matched = [i for i in self.items if i.get(pk_attr) == pk_value]
        matched.sort(key=lambda i: i.get(sort_attr, ""))
        if not kwargs.get("ScanIndexForward", True):
            matched.reverse()

        start = kwargs.get("ExclusiveStartKey")
        if start:
            for idx, item in enumerate(matched):
                if all(item.get(k) == start.get(k) for k in start):
                    matched = matched[idx + 1 :]
                    break
            else:
                matched = []

        limit = kwargs.get("Limit", len(matched))
        page = matched[:limit]
        resp: dict[str, Any] = {"Items": [dict(i) for i in page]}
        if len(matched) > limit and page:
            last = page[-1]
            resp["LastEvaluatedKey"] = {k: last[k] for k in key_attrs if k in last}
        return resp
