"""Shared CloudWatch custom metrics for the Agentics pipeline.

Thin wrapper around ``aws_lambda_powertools.metrics.single_metric`` so every
agent emits metrics under the same namespace with consistent dimensions.

Metrics published across the pipeline:

    AgentInvocations            Count          dims: agent, stage
    AgentDurationMs             Milliseconds   dims: agent, stage
    IteratorIterations          Count          dims: stage, outcome
    ValidatorPassRate           NoUnit         dims: validator, stage  (0.0 or 1.0)
    ClaudeInputTokens           Count          dims: agent, stage
    ClaudeOutputTokens          Count          dims: agent, stage
    ClaudeCacheReadTokens       Count          dims: agent, stage  (>0 = cache hit)
    ClaudeCacheCreationTokens   Count          dims: agent, stage  (>0 = cache write)

``namespace`` defaults to the POWERTOOLS_METRICS_NAMESPACE env var (set in
``template.yaml`` Globals). ``STAGE`` is read from the Lambda env.

We use ``single_metric`` (one EMF blob per metric) instead of a shared
Metrics() buffer so dimensions added for one metric don't leak into another.
"""

from __future__ import annotations

import os
import time
from functools import wraps
from typing import Any, Callable

from aws_lambda_powertools.metrics import MetricUnit, single_metric


_DEFAULT_NAMESPACE = os.getenv("POWERTOOLS_METRICS_NAMESPACE", "AgenticsCodeTeam")


def _stage() -> str:
    return os.getenv("STAGE", "dev")


def _emit(name: str, unit: MetricUnit, value: float, dimensions: dict[str, str]) -> None:
    with single_metric(
        name=name, unit=unit, value=value, namespace=_DEFAULT_NAMESPACE
    ) as metric:
        for k, v in dimensions.items():
            metric.add_dimension(name=k, value=v)


def emit_iterator_outcome(outcome: str) -> None:
    """Emit the IteratorIterations metric with the decision outcome."""
    _emit(
        "IteratorIterations",
        MetricUnit.Count,
        1,
        {"stage": _stage(), "outcome": outcome},
    )


def emit_validator_outcome(validator: str, passed: bool) -> None:
    """Emit the ValidatorPassRate metric (1.0 / 0.0) for a validator run."""
    _emit(
        "ValidatorPassRate",
        MetricUnit.NoUnit,
        1.0 if passed else 0.0,
        {"stage": _stage(), "validator": validator},
    )


def emit_claude_usage(agent: str, usage: Any) -> None:
    """Emit Claude API token-usage metrics for cost and caching observability.

    ``usage`` is the ``response.usage`` object from the Anthropic SDK. Its
    ``cache_*_input_tokens`` fields are only populated when prompt caching is
    actually used — otherwise they default to 0. A cache hit shows up as
    ``ClaudeCacheReadTokens > 0``; a cache write shows as
    ``ClaudeCacheCreationTokens > 0``.
    """
    dims = {"stage": _stage(), "agent": agent}
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    _emit("ClaudeInputTokens", MetricUnit.Count, input_tokens, dims)
    _emit("ClaudeOutputTokens", MetricUnit.Count, output_tokens, dims)
    _emit("ClaudeCacheReadTokens", MetricUnit.Count, cache_read, dims)
    _emit("ClaudeCacheCreationTokens", MetricUnit.Count, cache_creation, dims)


def instrument_agent(agent_name: str) -> Callable:
    """Decorator that emits AgentInvocations + AgentDurationMs per handler call.

    Emits on both normal return and exception paths. The handler's signature
    and return value are preserved.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(event: dict, context: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return func(event, context, *args, **kwargs)
            finally:
                duration_ms = (time.perf_counter() - started) * 1000.0
                dims = {"stage": _stage(), "agent": agent_name}
                _emit("AgentInvocations", MetricUnit.Count, 1, dims)
                _emit("AgentDurationMs", MetricUnit.Milliseconds, duration_ms, dims)

        return wrapper

    return decorator
