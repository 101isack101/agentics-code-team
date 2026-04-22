"""Unit tests for the shared Claude client wrapper.

Focus: caching wiring, usage metrics emission, and API compatibility with the
legacy call sites. Does NOT exercise the real Anthropic SDK — the `Anthropic`
client is injected as a mock via the `client=` kwarg.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import claude_client


def _mock_anthropic_response(text: str = "ok", usage: dict | None = None) -> MagicMock:
    """Build a fake Anthropic SDK response with text block + optional usage."""
    usage_obj = SimpleNamespace(
        input_tokens=(usage or {}).get("input_tokens", 100),
        output_tokens=(usage or {}).get("output_tokens", 50),
        cache_read_input_tokens=(usage or {}).get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=(usage or {}).get("cache_creation_input_tokens", 0),
    )
    text_block = SimpleNamespace(type="text", text=text)
    resp = MagicMock()
    resp.content = [text_block]
    resp.usage = usage_obj
    return resp


@pytest.fixture
def fake_anthropic():
    """Return a mock Anthropic SDK client."""
    client = MagicMock()
    client.messages.create.return_value = _mock_anthropic_response("hello")
    return client


def test_complete_returns_concatenated_text(fake_anthropic):
    cc = claude_client.ClaudeClient(model="test-model", client=fake_anthropic)
    out = cc.complete(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert out == "hello"


def test_complete_forwards_max_tokens_and_model(fake_anthropic):
    cc = claude_client.ClaudeClient(model="test-model", client=fake_anthropic)
    cc.complete(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=4242,
    )
    kwargs = fake_anthropic.messages.create.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["max_tokens"] == 4242


def test_complete_calls_inject_cache_control_when_enabled(fake_anthropic):
    """When enable_cache=True, the helper _inject_cache_control is invoked."""
    cc = claude_client.ClaudeClient(model="test-model", client=fake_anthropic)
    messages = [{"role": "user", "content": "hi"}]

    with patch.object(
        claude_client, "_inject_cache_control", wraps=claude_client._inject_cache_control
    ) as spy:
        cc.complete(system="sys", messages=messages, enable_cache=True)
    spy.assert_called_once()


def test_complete_skips_inject_cache_control_when_disabled(fake_anthropic):
    """enable_cache=False bypasses the helper entirely."""
    cc = claude_client.ClaudeClient(model="test-model", client=fake_anthropic)

    with patch.object(claude_client, "_inject_cache_control") as spy:
        cc.complete(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            enable_cache=False,
        )
    spy.assert_not_called()


def test_complete_does_not_mutate_caller_messages(fake_anthropic):
    """Deepcopy guarantees: the caller's `messages` list is untouched."""
    cc = claude_client.ClaudeClient(model="test-model", client=fake_anthropic)
    messages = [{"role": "user", "content": "hi"}]
    original = [dict(m) for m in messages]

    cc.complete(system="sys", messages=messages, enable_cache=True)
    assert messages == original


def test_complete_emits_usage_metrics_when_agent_name_passed(fake_anthropic):
    fake_anthropic.messages.create.return_value = _mock_anthropic_response(
        "ok",
        usage={
            "input_tokens": 1234,
            "output_tokens": 56,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 400,
        },
    )
    cc = claude_client.ClaudeClient(model="test-model", client=fake_anthropic)

    with patch.object(claude_client, "emit_claude_usage") as spy:
        cc.complete(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            agent_name="spec_agent",
        )
    spy.assert_called_once()
    agent_arg, usage_arg = spy.call_args.args
    assert agent_arg == "spec_agent"
    assert usage_arg.input_tokens == 1234
    assert usage_arg.cache_read_input_tokens == 800


def test_complete_no_metrics_when_agent_name_omitted(fake_anthropic):
    cc = claude_client.ClaudeClient(model="test-model", client=fake_anthropic)
    with patch.object(claude_client, "emit_claude_usage") as spy:
        cc.complete(system="sys", messages=[{"role": "user", "content": "hi"}])
    spy.assert_not_called()


def test_metrics_failure_does_not_break_call(fake_anthropic):
    """Observability must never bring down the actual Claude call."""
    cc = claude_client.ClaudeClient(model="test-model", client=fake_anthropic)
    with patch.object(
        claude_client, "emit_claude_usage", side_effect=RuntimeError("CW down")
    ):
        out = cc.complete(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            agent_name="spec_agent",
        )
    assert out == "hello"


def test_to_blocks_normalizes_string_content():
    assert claude_client._to_blocks("hello") == [{"type": "text", "text": "hello"}]


def test_to_blocks_copies_list_content():
    src = [{"type": "text", "text": "hi"}]
    out = claude_client._to_blocks(src)
    assert out == src
    assert out is not src
    assert out[0] is not src[0]


def test_inject_cache_control_marks_first_user_last_block():
    """Strategy 'first user only': cache_control on last block of messages[0]."""
    messages = [
        {"role": "user", "content": "spec + files"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "retry"},
    ]
    system = "sys"

    _, out = claude_client._inject_cache_control(messages, system)

    # First user message: content now list-of-blocks with cache_control on last.
    first_content = out[0]["content"]
    assert isinstance(first_content, list)
    assert first_content[-1] == {
        "type": "text",
        "text": "spec + files",
        "cache_control": {"type": "ephemeral"},
    }
    # Second user message: unchanged (still a plain string).
    assert out[2]["content"] == "retry"


def test_inject_cache_control_preserves_existing_blocks():
    """If first user already has multi-block content, mark only the LAST block."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "part A"},
                {"type": "text", "text": "part B"},
            ],
        }
    ]
    _, out = claude_client._inject_cache_control(messages, "sys")
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "part A"}  # untouched
    assert blocks[1] == {
        "type": "text",
        "text": "part B",
        "cache_control": {"type": "ephemeral"},
    }


def test_inject_cache_control_handles_empty_messages():
    """Edge case: empty message list must not raise."""
    sys_out, msgs_out = claude_client._inject_cache_control([], "sys")
    assert sys_out == "sys"
    assert msgs_out == []
