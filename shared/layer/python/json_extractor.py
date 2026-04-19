"""Defensive JSON extraction for Claude responses.

Claude usually returns raw JSON but sometimes wraps output in a ```json fence
despite system-prompt instructions. This helper normalizes both cases so
callers can trust a single entry point.
"""

from __future__ import annotations

import json
from typing import Any


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    return json.loads(stripped)
