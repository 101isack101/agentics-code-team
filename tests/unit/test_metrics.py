"""Unit tests for the shared metrics module.

Asserts the decorator preserves handler semantics AND that the four helper
emitters route to ``aws_lambda_powertools.metrics.single_metric`` with the
correct name, unit, value and dimensions.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import metrics


@pytest.fixture
def fake_single_metric():
    """Patch single_metric so we can inspect the calls."""
    with patch.object(metrics, "single_metric") as mock:
        cm = MagicMock()
        mock.return_value.__enter__.return_value = cm
        mock.return_value.__exit__.return_value = False
        yield mock, cm


def test_decorator_preserves_return_value(fake_single_metric, monkeypatch):
    monkeypatch.setenv("STAGE", "dev")

    @metrics.instrument_agent("probe_agent")
    def handler(event, context):
        return {"ok": True, "echo": event["x"]}

    out = handler({"x": 42}, context=None)
    assert out == {"ok": True, "echo": 42}


def test_decorator_emits_two_metrics_per_call(fake_single_metric, monkeypatch):
    mock_single, metric_cm = fake_single_metric
    monkeypatch.setenv("STAGE", "prod")

    @metrics.instrument_agent("probe_agent")
    def handler(event, context):
        return "ok"

    handler({}, context=None)

    names = [call.kwargs["name"] for call in mock_single.call_args_list]
    assert names == ["AgentInvocations", "AgentDurationMs"]
    # Dimensions include both stage and agent for each call.
    dim_calls = [c.kwargs for c in metric_cm.add_dimension.call_args_list]
    keys = {c["name"] for c in dim_calls}
    values = {c["value"] for c in dim_calls}
    assert {"stage", "agent"} <= keys
    assert "prod" in values and "probe_agent" in values


def test_decorator_still_emits_on_exception(fake_single_metric):
    mock_single, _ = fake_single_metric

    @metrics.instrument_agent("probe_agent")
    def handler(event, context):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        handler({}, context=None)
    # Still 2 emissions even though handler raised.
    assert mock_single.call_count == 2


def test_emit_validator_and_iterator_outcomes(fake_single_metric, monkeypatch):
    mock_single, metric_cm = fake_single_metric
    monkeypatch.setenv("STAGE", "dev")

    metrics.emit_validator_outcome("security", passed=True)
    metrics.emit_iterator_outcome("exhausted")

    names = [c.kwargs["name"] for c in mock_single.call_args_list]
    values = [c.kwargs["value"] for c in mock_single.call_args_list]
    assert "ValidatorPassRate" in names
    assert "IteratorIterations" in names
    assert 1.0 in values and 1 in values
    added = [c.kwargs for c in metric_cm.add_dimension.call_args_list]
    outcome_dim = [c for c in added if c["name"] == "outcome"]
    assert outcome_dim and outcome_dim[0]["value"] == "exhausted"


def test_emit_claude_usage_publishes_four_metrics(fake_single_metric, monkeypatch):
    mock_single, metric_cm = fake_single_metric
    monkeypatch.setenv("STAGE", "dev")
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=750,
        cache_creation_input_tokens=100,
    )

    metrics.emit_claude_usage("codegen_agent", usage)

    names = [c.kwargs["name"] for c in mock_single.call_args_list]
    values = [c.kwargs["value"] for c in mock_single.call_args_list]
    assert names == [
        "ClaudeInputTokens",
        "ClaudeOutputTokens",
        "ClaudeCacheReadTokens",
        "ClaudeCacheCreationTokens",
    ]
    assert values == [1000, 200, 750, 100]
    dim_calls = [c.kwargs for c in metric_cm.add_dimension.call_args_list]
    agent_dims = [c for c in dim_calls if c["name"] == "agent"]
    assert agent_dims and all(d["value"] == "codegen_agent" for d in agent_dims)


def test_emit_claude_usage_handles_missing_cache_fields(fake_single_metric):
    """When caching isn't used, cache_*_input_tokens may be None or missing."""
    mock_single, _ = fake_single_metric
    # Usage object without cache fields (older SDK or caching disabled).
    usage = SimpleNamespace(input_tokens=100, output_tokens=50)

    metrics.emit_claude_usage("spec_agent", usage)

    values = [c.kwargs["value"] for c in mock_single.call_args_list]
    assert values == [100, 50, 0, 0]
