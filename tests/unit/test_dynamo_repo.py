"""Tests for optimistic locking in RunsRepository."""

from __future__ import annotations

import pytest

from dynamo_repo import ConcurrentModificationError, RunsRepository


def test_create_item_writes_version_1(runs_table):
    repo = RunsRepository()
    item = repo.create_item(
        run_id="run-001",
        item_type="SPEC",
        status="SPEC_READY",
        payload={"spec_s3_uri": "s3://x/y.json"},
    )
    assert item["version"] == 1
    assert item["status"] == "SPEC_READY"

    fetched = repo.get_item("run-001", "SPEC")
    assert fetched["version"] == 1


def test_create_item_twice_raises(runs_table):
    repo = RunsRepository()
    repo.create_item(
        run_id="run-002", item_type="SPEC", status="S", payload={}
    )
    with pytest.raises(ConcurrentModificationError):
        repo.create_item(
            run_id="run-002", item_type="SPEC", status="S", payload={}
        )


def test_update_bumps_version(runs_table):
    repo = RunsRepository()
    repo.create_item(
        run_id="run-003", item_type="SPEC", status="SPEC_READY", payload={"a": 1}
    )
    updated = repo.update_item(
        run_id="run-003",
        item_type="SPEC",
        expected_version=1,
        status="SPEC_IN_REVIEW",
        payload={"a": 2},
    )
    assert updated["version"] == 2
    assert updated["status"] == "SPEC_IN_REVIEW"
    assert updated["payload"]["a"] == 2


def test_update_with_stale_version_raises(runs_table):
    repo = RunsRepository()
    repo.create_item(
        run_id="run-004", item_type="SPEC", status="S", payload={}
    )
    repo.update_item(
        run_id="run-004",
        item_type="SPEC",
        expected_version=1,
        status="S2",
        payload={},
    )
    with pytest.raises(ConcurrentModificationError):
        repo.update_item(
            run_id="run-004",
            item_type="SPEC",
            expected_version=1,
            status="S3",
            payload={},
        )
