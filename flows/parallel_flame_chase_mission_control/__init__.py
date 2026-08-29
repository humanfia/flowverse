"""Mission Control Parallel Flame Chase with a first-start audit policy."""

from __future__ import annotations

from typing import Any

from _parallel_flame_chase.core.api import BaseConfig, OrchestrateorAgents
from hmz.flows import flow
from pydantic import Field

from parallel_flame_chase_mission_control.runtime import execute

Agents = OrchestrateorAgents


class Config(BaseConfig):
    """Fallback mission pacing and audit interruption controls."""

    experiment_time_budget_hours: float | None = Field(
        default=None,
        gt=0,
        le=720,
        description="Known outer experiment budget supplied to the initial orchestrateor.",
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
    """Run lanes under the orchestrateor's persisted audit-control policy."""
    execute(agents, task, config or Config(), state)


__all__ = ["Agents", "Config", "run"]
