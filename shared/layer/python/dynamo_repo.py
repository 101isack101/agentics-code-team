"""DynamoDB repository with optimistic locking.

Schema (all items share this layout):
    run_id      (S) — PK
    item_type   (S) — SK. e.g. "RUN", "SPEC", "GEN#v1", "REVIEW#v1"
    version     (N) — monotonic counter used for optimistic locking
    status      (S) — GSI1 PK  (e.g. SPEC_READY, GEN_IN_PROGRESS, DONE, FAILED)
    updated_at  (S) — GSI1 SK, ISO-8601 UTC
    created_at  (S) — ISO-8601 UTC
    payload     (M) — free-form map with the actual data for this item

Optimistic locking contract:
    - create_item: writes version=1 using attribute_not_exists(run_id).
    - update_item: bumps version conditionally (version = :expected).
    - A stale writer raises ConcurrentModificationError so the caller
      (or Step Functions retry) can reload and retry.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError


class ConcurrentModificationError(RuntimeError):
    """Raised when an optimistic-locking conditional update fails."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunsRepository:
    def __init__(
        self,
        table_name: str | None = None,
        dynamodb_resource: Any = None,
    ) -> None:
        self.table_name = table_name or os.environ["RUNS_TABLE"]
        self._dynamodb = dynamodb_resource or boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    def create_item(
        self,
        run_id: str,
        item_type: str,
        status: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = _utcnow_iso()
        item = {
            "run_id": run_id,
            "item_type": item_type,
            "version": 1,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "payload": payload,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(run_id) AND attribute_not_exists(item_type)"
                ),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConcurrentModificationError(
                    f"Item already exists: run_id={run_id} item_type={item_type}"
                ) from exc
            raise
        return item

    def get_item(self, run_id: str, item_type: str) -> dict[str, Any] | None:
        response = self._table.get_item(
            Key={"run_id": run_id, "item_type": item_type},
            ConsistentRead=True,
        )
        return response.get("Item")

    def update_item(
        self,
        run_id: str,
        item_type: str,
        expected_version: int,
        status: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = _utcnow_iso()
        new_version = expected_version + 1
        try:
            response = self._table.update_item(
                Key={"run_id": run_id, "item_type": item_type},
                UpdateExpression=(
                    "SET #v = :new_version, "
                    "#s = :status, "
                    "#u = :updated_at, "
                    "#p = :payload"
                ),
                ConditionExpression="#v = :expected_version",
                ExpressionAttributeNames={
                    "#v": "version",
                    "#s": "status",
                    "#u": "updated_at",
                    "#p": "payload",
                },
                ExpressionAttributeValues={
                    ":new_version": new_version,
                    ":expected_version": expected_version,
                    ":status": status,
                    ":updated_at": now,
                    ":payload": payload,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConcurrentModificationError(
                    f"Optimistic lock failed for run_id={run_id} "
                    f"item_type={item_type} expected_version={expected_version}"
                ) from exc
            raise
        return response["Attributes"]
