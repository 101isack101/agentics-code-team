"""S3 repository for artifacts with versioning.

Key layout:
    sources/<run_id>/source.zip
    specs/<run_id>/technical_spec.json
    artifacts/<run_id>/v<n>/<filename>
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3


class ArtifactsRepository:
    def __init__(
        self,
        bucket_name: str | None = None,
        s3_client: Any = None,
    ) -> None:
        self.bucket_name = bucket_name or os.environ["ARTIFACTS_BUCKET"]
        self._s3 = s3_client or boto3.client("s3")

    def put_json(self, key: str, data: dict[str, Any]) -> str:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._s3.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            ServerSideEncryption="AES256",
        )
        return f"s3://{self.bucket_name}/{key}"

    def get_json(self, key: str) -> dict[str, Any]:
        response = self._s3.get_object(Bucket=self.bucket_name, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))

    def spec_key(self, run_id: str) -> str:
        return f"specs/{run_id}/technical_spec.json"

    def artifact_key(self, run_id: str, version: int, filename: str) -> str:
        return f"artifacts/{run_id}/v{version}/{filename}"

    def source_key(self, run_id: str) -> str:
        return f"sources/{run_id}/source.zip"
