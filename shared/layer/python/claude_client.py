"""Thin wrapper around the Anthropic SDK, shared by all agent Lambdas.

Responsibilities:
- Resolve the API key from Secrets Manager (cached across warm invocations).
- Call Claude with system + user prompts, optionally with prompt caching.
- Emit token-usage metrics (input/output/cache_read/cache_creation) per call.
- Return the raw text content for the caller to parse as JSON.

Lives in the shared Lambda Layer so every agent imports the same client
without duplicating the anthropic package, the secret-lookup logic, or the
caching convention.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from functools import lru_cache

import boto3
from anthropic import Anthropic

from metrics import emit_claude_usage


logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8000


@lru_cache(maxsize=1)
def _load_api_key() -> str:
    secret_arn = os.environ["ANTHROPIC_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])
    return secret["ANTHROPIC_API_KEY"]


def _to_blocks(content) -> list[dict]:
    """Normalize message content (str or list of blocks) into list-of-blocks form."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [dict(block) for block in content]


def _inject_cache_control(
    messages: list[dict],
    system: str,
) -> tuple[list[dict] | str, list[dict]]:
    """Inject `cache_control: {"type": "ephemeral"}` breakpoint(s) into the request.

    Anthropic caches everything in the prefix UP TO (and including) a content
    block marked with cache_control. Breakpoints: 4 max per request. For Sonnet
    4.6 the prefix must be >=1024 tokens to actually cache — below that the
    cache_control is ignored and no cache is created.

    Our agents have system prompts of 550-700 tokens (below the 1024 min), but
    user messages with the spec + files are typically 2000-10000 tokens — so
    a breakpoint in the user message caches `system + user[0]` together and
    crosses the threshold.

    BREAKPOINT STRATEGY — three options with different tradeoffs:

    1. "first user only" — breakpoint on LAST block of messages[0]
       Caches: system + first user message
       Best for: JSON retries in codegen (same prefix re-sent)
       Simplest; recommended starting point.

    2. "first + last user" — two breakpoints, on messages[0] and messages[-1]
       Caches: everything up through the latest user message
       Best for: long conversations where assistant responses grow
       Slightly more expensive on first call (two cache writes).

    3. "last user only" — breakpoint on LAST block of messages[-1]
       Caches: entire conversation prefix
       Best for: long multi-turn loops where caching the whole history wins

    ---

    TODO (Isaac — ~5-8 lines):
    Implement the breakpoint logic below. The simplest version (Option 1):

        first = messages[0]
        blocks = _to_blocks(first["content"])
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        new_messages = [{**first, "content": blocks}] + messages[1:]
        return system, new_messages

    You can also add a breakpoint on the system prompt — but since each agent's
    system is <1024 tokens alone, it only helps as a contributor to the prefix.
    """
    # Strategy: "first user only" — breakpoint on last block of messages[0].
    # Caches `system + user[0]` together (codegen JSON retries benefit most).
    if not messages:
        return system, messages
    first = messages[0]
    blocks = _to_blocks(first["content"])
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    new_messages = [{**first, "content": blocks}, *messages[1:]]
    return system, new_messages


class ClaudeClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: Anthropic | None = None,
    ) -> None:
        self.model = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        self._client = client or Anthropic(api_key=api_key or _load_api_key())

    def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        agent_name: str | None = None,
        enable_cache: bool = True,
    ) -> str:
        sys_param: list[dict] | str = system
        msgs = messages

        if enable_cache:
            msgs = copy.deepcopy(messages)
            sys_param, msgs = _inject_cache_control(msgs, system)

        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=sys_param,
            messages=msgs,
        )

        if agent_name:
            try:
                emit_claude_usage(agent_name, response.usage)
            except Exception:
                logger.exception("claude_usage_emit_failed")

        parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts).strip()
