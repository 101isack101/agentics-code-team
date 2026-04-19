"""Schema-validates the three canned simulation events.

Guarantees any JSON we ship under tests/simulations/ is a valid
SpecAgentEvent — catches drift before `sam local invoke` runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schemas import SpecAgentEvent


SIMULATIONS_DIR = Path(__file__).parent
SIM_FILES = sorted(SIMULATIONS_DIR.glob("0*.json"))


@pytest.mark.parametrize("sim_path", SIM_FILES, ids=lambda p: p.stem)
def test_simulation_event_is_valid(sim_path: Path) -> None:
    payload = json.loads(sim_path.read_text(encoding="utf-8"))
    event = SpecAgentEvent.model_validate(payload)
    assert event.run_id == payload["run_id"]
    assert len(event.requirements) >= 20


def test_three_simulations_present() -> None:
    assert len(SIM_FILES) == 3, f"expected 3 simulation files, got {len(SIM_FILES)}"
