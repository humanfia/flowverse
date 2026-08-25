"""Public agent and configuration types for Parallel Flame Chase."""

from __future__ import annotations

from typing import Annotated, Literal, NamedTuple, TypeAlias

from hmz.flows import Agent, AgentDefaults
from pydantic import BaseModel, Field

NoGoals: TypeAlias = Annotated[Agent, AgentDefaults(goals=False)]


class Agents(NamedTuple):
    """One coordinator and two alternating actors for each fixed lane."""

    coordinator: NoGoals
    lane_1_actor_a: NoGoals
    lane_1_actor_b: NoGoals
    lane_2_actor_a: NoGoals
    lane_2_actor_b: NoGoals
    lane_3_actor_a: NoGoals
    lane_3_actor_b: NoGoals


class BaseConfig(BaseModel):
    """Pacing and resume policy shared by both schedulers."""

    model_config = {"extra": "forbid"}

    rest_seconds: float = Field(
        default=1.0,
        ge=0.05,
        le=60.0,
        description="Seconds the single-writer scheduler rests between control passes.",
    )
    resume_mode: Literal["auto", "fresh"] = Field(
        default="auto",
        description="Resume compatible Humanize state, or deliberately start a fresh run.",
    )
