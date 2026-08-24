"""Mission Parallel Flame Chase -- three lanes governed by scoped evidence audits.

    hmz exec -f ./flows/parallel_flame_chase_mission \
      -a coordinator -a lane1-a -a lane1-b -a lane2-a -a lane2-b \
      -a lane3-a -a lane3-b "$(cat TASK.md)"

Lane 1 alone owns the original source while Lanes 2 and 3 work in private snapshots. A fresh
coordinator adjudicates terminal outcomes, deadlines, stalls, failures, objective revisions,
external review requests, and periodic portfolio audits. Accepted private-lane artifacts enter
Lane 1's durable integration queue.

This is a separate public flow from ``parallel_flame_chase``. It has its own configuration,
mounted mission skill, and resumable Humanize state; the two flows share only a hidden runtime
implementation so their common isolation and recovery semantics cannot drift.
"""

from __future__ import annotations

from typing import Any

from _parallel_flame_chase.core.api import Agents, BaseConfig
from hmz.flows import flow
from pydantic import Field


class Config(BaseConfig):
    """Isolation, mission cadence, interruption, and resume policy."""

    global_audit_hours: float | None = Field(
        default=6.0,
        ge=0.25,
        le=168.0,
        description="Hours between global portfolio audits; null disables the periodic audit.",
    )
    mission_deadline_hours: float = Field(
        default=6.0,
        ge=0.25,
        le=168.0,
        description="Fallback deadline for coordinator missions that omit a usable value.",
    )
    max_turns_without_outcome: int = Field(
        default=6,
        ge=1,
        le=50,
        description="Fallback progress-turn limit before a targeted audit.",
    )
    interrupt_grace_seconds: float = Field(
        default=60.0,
        ge=0.0,
        le=600.0,
        description="Grace after audit interjection before closing only the target session.",
    )
    external_events: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional adapter-owned version-1 JSONL evidence stream.",
    )


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Run mission-governed lanes with scoped audits and integration handoff."""
    from .runtime.engine import execute

    execute(agents, task, config or Config(), state)


__all__ = ["Agents", "Config", "run"]
