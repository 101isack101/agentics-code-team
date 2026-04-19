"""Thin wrapper around the Anthropic SDK, shared by all agent Lambdas.

Responsibilities:
- Resolve the API key from Secrets Manager (cached across warm invocations).
- Call Claude with system + user prompts.
- Return the raw text content for the caller to parse as JSON.

Lives in the shared Lambda Layer so every agent imports the same client
without duplicating the anthropic package or the secret-lookup logic.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3
from anthropic import Anthropic


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8000


@lru_cache(maxsize=1)
def _load_api_key() -> str:
    secret_arn = os.environ["ANTHROPIC_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])
    return secret["ANTHROPIC_API_KEY"]


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
    ) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts).strip()
