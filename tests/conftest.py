"""Pytest fixtures shared across unit tests.

Adds the shared layer and each Lambda package to sys.path so tests can import
modules the same way the Lambda runtime does (layers are merged into /opt/python
at runtime, which is effectively sys.path[0]).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Disable powertools Tracer before any test module imports `app`
# (Tracer() is built at import time and would otherwise try to load
# aws-xray-sdk, which is only present in the Lambda runtime).
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "true")

import boto3
import pytest
from moto import mock_aws


ROOT = Path(__file__).resolve().parent.parent
SHARED_LAYER = ROOT / "shared" / "layer" / "python"
SPEC_AGENT = ROOT / "lambdas" / "spec_agent"

for path in (SHARED_LAYER, SPEC_AGENT):
    sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("ARTIFACTS_BUCKET", "agentics-test-bucket")
    monkeypatch.setenv("RUNS_TABLE", "agentics-runs-test")
    monkeypatch.setenv("ANTHROPIC_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:000000000000:secret:test")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "spec-agent-test")
    monkeypatch.setenv("POWERTOOLS_TRACE_DISABLED", "true")


@pytest.fixture
def aws_mocks():
    with mock_aws():
        yield


@pytest.fixture
def s3_bucket(aws_mocks):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=os.environ["ARTIFACTS_BUCKET"])
    return os.environ["ARTIFACTS_BUCKET"]


@pytest.fixture
def lambda_context():
    class _Ctx:
        function_name = "spec-agent-test"
        memory_limit_in_mb = 1024
        invoked_function_arn = (
            "arn:aws:lambda:us-east-1:000000000000:function:spec-agent-test"
        )
        aws_request_id = "00000000-0000-0000-0000-000000000000"
        log_group_name = "/aws/lambda/spec-agent-test"
        log_stream_name = "test-stream"
    return _Ctx()


@pytest.fixture
def runs_table(aws_mocks):
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName=os.environ["RUNS_TABLE"],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "item_type", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "item_type", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1-Status-UpdatedAt",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return os.environ["RUNS_TABLE"]
