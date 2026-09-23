"""Shared public types for independently registered parallel flows."""

from typing import Annotated, Literal, NamedTuple, TypeAlias

from hmz.flows import Agent, AgentDefaults, Person
from pydantic import BaseModel, Field

NoGoals: TypeAlias = Annotated[Agent, AgentDefaults(goals=False)]
DEFAULT_WORKSPACE_FILE_WARNING_THRESHOLD = 5_000
DEFAULT_WORKSPACE_COPY_WARNING_THRESHOLD_BYTES = 1024**3


class Agents(NamedTuple):
    """One coordinator, two alternating actors per lane, and the prompt's person."""

    coordinator: NoGoals
    lane_1_actor_a: NoGoals
    lane_1_actor_b: NoGoals
    lane_2_actor_a: NoGoals
    lane_2_actor_b: NoGoals
    lane_3_actor_a: NoGoals
    lane_3_actor_b: NoGoals
    human: Person


class OrchestrateorAgents(NamedTuple):
    """Additive-variant topology with the planning role named orchestrateor."""

    orchestrateor: NoGoals
    lane_1_actor_a: NoGoals
    lane_1_actor_b: NoGoals
    lane_2_actor_a: NoGoals
    lane_2_actor_b: NoGoals
    lane_3_actor_a: NoGoals
    lane_3_actor_b: NoGoals

    @property
    def coordinator(self) -> NoGoals:
        """Keep the shared runtime compatible without exposing the old role name."""
        return self.orchestrateor


class GitPRAgents(NamedTuple):
    """Git/PR topology with one planner, six actors, and the prompt's person."""

    orchestrateor: NoGoals
    lane_1_actor_a: NoGoals
    lane_1_actor_b: NoGoals
    lane_2_actor_a: NoGoals
    lane_2_actor_b: NoGoals
    lane_3_actor_a: NoGoals
    lane_3_actor_b: NoGoals
    human: Person

    @property
    def coordinator(self) -> NoGoals:
        """Adapt the historical spelling to the shared scheduler interface."""
        return self.orchestrateor


class BaseConfig(BaseModel):
    """Workspace-copy safety, pacing, and resume policy shared by the schedulers."""

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
    confirm_large_workspace_copies: bool = Field(
        default=False,
        description=(
            "Ask before materializing an oversized workspace; otherwise warn and continue."
        ),
    )
    workspace_file_warning_threshold: int = Field(
        default=DEFAULT_WORKSPACE_FILE_WARNING_THRESHOLD,
        ge=1,
        le=100_000_000,
        description="Regular-file count that marks a source workspace as oversized.",
    )
    workspace_copy_warning_threshold_bytes: int = Field(
        default=DEFAULT_WORKSPACE_COPY_WARNING_THRESHOLD_BYTES,
        ge=1,
        le=1_000_000_000_000_000,
        description=(
            "Estimated bytes across new workspace materializations that trigger a warning."
        ),
    )
