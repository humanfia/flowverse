"""Shared public types for the two independently registered flows."""

# The flow loader resolves this shared NamedTuple with the entry point's globals. Evaluate the
# annotations here so the injected Person remains distinct from the selectable coding agents.
from typing import Annotated, Literal, NamedTuple, TypeAlias

from hmz.flows import Agent, AgentDefaults, Person
from pydantic import BaseModel, Field

NoGoals: TypeAlias = Annotated[Agent, AgentDefaults(goals=False)]
DEFAULT_WORKSPACE_FILE_WARNING_THRESHOLD = 5_000


class Agents(NamedTuple):
    """One coordinator, two alternating actors per lane, and the person at the prompt."""

    coordinator: NoGoals
    lane_1_actor_a: NoGoals
    lane_1_actor_b: NoGoals
    lane_2_actor_a: NoGoals
    lane_2_actor_b: NoGoals
    lane_3_actor_a: NoGoals
    lane_3_actor_b: NoGoals
    human: Person


class BaseConfig(BaseModel):
    """Snapshot safety, pacing, and resume policy shared by both schedulers."""

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
    workspace_file_warning_threshold: int = Field(
        default=DEFAULT_WORKSPACE_FILE_WARNING_THRESHOLD,
        ge=1,
        le=100_000_000,
        description=(
            "Regular-file count that triggers confirmation before creating workspace snapshots."
        ),
    )
