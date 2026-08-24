"""Parallel Flame Chase -- three alternating lanes and one planning coordinator.

Base mode plans once, then lets the three lanes coordinate through durable reports:

    hmz exec -f ./flows/parallel_flame_chase \
      -a coordinator -a lane1-a -a lane1-b -a lane2-a -a lane2-b \
      -a lane3-a -a lane3-b "$(cat TASK.md)"

Mission mode adds scoped, evidence-bound audits and a Lane 1 integration queue:

    hmz exec -f ./flows/parallel_flame_chase:mission \
      -a coordinator -a lane1-a -a lane1-b -a lane2-a -a lane2-b \
      -a lane3-a -a lane3-b "$(cat TASK.md)"

Both modes have a fixed topology: Lane 1 alone owns the original source while Lanes 2 and 3
work in private snapshots. The flow never executes remote releases, submissions, deployments,
messages, purchases, or other domain actions. Domain adapters may publish bounded evidence to
mission mode's versioned JSONL ingress; decisions and actions remain inside this flow.

Runs are resumable. Repeating the same task or entering ``continue`` resumes compatible state.
For ``continue``, a changed TASK.md is treated as an objective revision; a different substantive
task starts a fresh run. Set ``resume_mode: fresh`` to force a new run.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Annotated, Any, Literal, NamedTuple, TypeAlias

from _parallel_flame_chase.runtime import execute
from hmz.flows import Agent, AgentDefaults, flow
from pydantic import BaseModel, Field, field_validator

NoGoals: TypeAlias = Annotated[Agent, AgentDefaults(goals=False)]


class Agents(NamedTuple):
    """One coordinator followed by two alternating actors for each fixed lane."""

    coordinator: NoGoals
    lane_1_actor_a: NoGoals
    lane_1_actor_b: NoGoals
    lane_2_actor_a: NoGoals
    lane_2_actor_b: NoGoals
    lane_3_actor_a: NoGoals
    lane_3_actor_b: NoGoals


class Config(BaseModel):
    """Isolation, pacing, and resume policy shared by both public modes."""

    model_config = {"extra": "forbid"}

    rest_seconds: float = Field(
        default=1.0,
        ge=0.05,
        le=60.0,
        description="Seconds the single-writer scheduler rests between control passes.",
    )
    protected_paths: tuple[str, ...] = Field(
        default=(),
        max_length=50,
        description="Relative paths each lane may inspect but must not modify.",
    )
    resume_mode: Literal["auto", "fresh"] = Field(
        default="auto",
        description="Resume compatible Humanize state, or deliberately start a fresh run.",
    )

    @field_validator("protected_paths")
    @classmethod
    def safe_protected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("protected_paths must not contain duplicates")
        for raw in values:
            path = PurePath(raw)
            if not raw or path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"protected path must be non-empty and relative: {raw!r}"
                )
        return values


class MissionConfig(Config):
    """Generic mission deadlines, portfolio audit cadence, and interruption policy."""

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
    """Run three durable lanes after one read-only coordinator planning turn."""
    execute(agents, task, config or Config(), state, mission_mode=False)


@flow(name="mission", resumable=True)
def mission(
    agents: Agents,
    task: str,
    config: MissionConfig | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Run the same topology with scoped audits and accepted-result integration."""
    execute(agents, task, config or MissionConfig(), state, mission_mode=True)


__all__ = ["Agents", "Config", "MissionConfig", "mission", "run"]
