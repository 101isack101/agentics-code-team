"""S3 repository for artifacts with versioning.

Key layout:
    sources/<run_id>/source.zip
    specs/<run_id>/technical_spec.json
    artifacts/<run_id>/v<n>/<filename>
    artifacts/<run_id>/v<n>/manifest.json
    artifacts/<run_id>/v<n>/review.json
    artifacts/<run_id>/v<n>/security.json
    artifacts/<run_id>/v<n>/scalability.json
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
        return self.s3_uri(key)

    def put_text(self, key: str, content: str, content_type: str = "text/plain; charset=utf-8") -> str:
        self._s3.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        return self.s3_uri(key)

    def get_json(self, key: str) -> dict[str, Any]:
        response = self._s3.get_object(Bucket=self.bucket_name, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))

    def get_text(self, key: str) -> str:
        response = self._s3.get_object(Bucket=self.bucket_name, Key=key)
        return response["Body"].read().decode("utf-8")

    def s3_uri(self, key: str) -> str:
        return f"s3://{self.bucket_name}/{key}"

    def spec_key(self, run_id: str) -> str:
        return f"specs/{run_id}/technical_spec.json"

    def artifact_key(self, run_id: str, version: int, filename: str) -> str:
        return f"artifacts/{run_id}/v{version}/{filename}"

    def manifest_key(self, run_id: str, version: int) -> str:
        return self.artifact_key(run_id, version, "manifest.json")

    def report_key(self, run_id: str, version: int, report_filename: str) -> str:
        return self.artifact_key(run_id, version, report_filename)

    def source_key(self, run_id: str) -> str:
        return f"sources/{run_id}/source.zip"

    @staticmethod
    def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
        if not s3_uri.startswith("s3://"):
            raise ValueError(f"Invalid S3 URI: {s3_uri}")
        without_scheme = s3_uri[5:]
        bucket, _, key = without_scheme.partition("/")
        if not bucket or not key:
            raise ValueError(f"Invalid S3 URI: {s3_uri}")
        return bucket, key
