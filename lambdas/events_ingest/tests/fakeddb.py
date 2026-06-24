"""Tabla DynamoDB falsa en memoria con semántica de conditional put / update.

Reproduce SÓLO lo que la ingesta necesita: ``put_item`` con
``attribute_not_exists(PK) AND attribute_not_exists(SK)`` y ``update_item`` con
``ConditionExpression="clip_status = :p"``. Lanza el MISMO ``ClientError`` que boto3
(``ConditionalCheckFailedException``) para ejercitar el camino dup del handler sin AWS.
"""

from __future__ import annotations

from botocore.exceptions import ClientError


def conditional_check_failed(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "cond"}},
        op,
    )


class FakeTable:
    def __init__(self):
        self.items: dict[tuple, dict] = {}
        self.put_calls = 0
        self.update_calls = 0

    def put_item(self, Item, ConditionExpression=None, **_kw):  # noqa: N803
        self.put_calls += 1
        key = (Item["PK"], Item["SK"])
        if (
            ConditionExpression
            and "attribute_not_exists(PK)" in ConditionExpression
            and "attribute_not_exists(SK)" in ConditionExpression
            and key in self.items
        ):
            raise conditional_check_failed("PutItem")
        self.items[key] = dict(Item)

    def update_item(
        self,
        Key,  # noqa: N803
        UpdateExpression,  # noqa: N803
        ConditionExpression=None,  # noqa: N803
        ExpressionAttributeValues=None,  # noqa: N803
        ExpressionAttributeNames=None,  # noqa: N803
        **_kw,
    ):
        self.update_calls += 1
        key = (Key["PK"], Key["SK"])
        current = self.items.get(key)
        if current is None:
            raise conditional_check_failed("UpdateItem")
        if ConditionExpression == "clip_status = :p":
            expected = ExpressionAttributeValues[":p"]
            if current.get("clip_status") != expected:
                raise conditional_check_failed("UpdateItem")
            current["clip_status"] = ExpressionAttributeValues[":u"]
        else:  # pragma: no cover - otras condiciones no usadas por la ingesta
            raise AssertionError(f"condición no soportada: {ConditionExpression!r}")
