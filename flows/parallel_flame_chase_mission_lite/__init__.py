"""Mission Lite Parallel Flame Chase with best-gated global audits.

    hmz exec -f ./flows/parallel_flame_chase_mission_lite \
      -a orchestrateor -a lane1-a -a lane1-b -a lane2-a -a lane2-b \
      -a lane3-a -a lane3-b "$(cat TASK.md)"

This is an additive experimental flow. It does not replace either existing Parallel Flame Chase
entry. Terminal outcomes still receive a targeted audit so missions can be accepted, redirected,
or continued, while a candidate that refreshes the primary shared best escalates review globally.
"""

from __future__ import annotations

from typing import Any

from _parallel_flame_chase.core.api import BaseConfig, OrchestrateorAgents
from hmz.flows import flow
from pydantic import Field

from parallel_flame_chase_mission_lite.runtime import execute

Agents = OrchestrateorAgents


class Config(BaseConfig):
    """Mission Lite cadence, interruption, and resume policy."""

    global_audit_hours: float | None = Field(
        default=6.0,
        ge=0.25,
        le=168.0,
        description="Hours between explicit periodic global portfolio audits.",
    )
    mission_deadline_hours: float = Field(default=6.0, ge=0.25, le=168.0)
    max_turns_without_outcome: int = Field(default=6, ge=1, le=50)
    interrupt_grace_seconds: float = Field(default=60.0, ge=0.0, le=600.0)
    external_events: str | None = Field(default=None, max_length=2000)


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Run best-gated global Mission audits without changing the original flow."""
    execute(agents, task, config or Config(), state)


__all__ = ["Agents", "Config", "run"]
