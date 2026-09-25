from typing import Literal

from hmz.flows import (
    Agent,
    AgentCollection,
    Env,
    EnvCollection,
    FilesEnvMixin,
    FlowParams,
    LocalEnv,
    Outworlder,
    Permission,
    PermissionKind,
    ScratchDirEnvMixin,
    ShellEnvMixin,
    TemporaryClonedDirEnvMixin,
)
from pydantic import Field

DEFAULT_WORKSPACE_FILE_WARNING_THRESHOLD = 5_000
DEFAULT_WORKSPACE_COPY_WARNING_THRESHOLD_BYTES = 1024**3
SKILL = "parallel-flame-chase"


class Actor(Agent):
    """A coordinator or lane actor: no `/goal`, the flow's skill, and full local reach.

    Lanes write artifacts and checkpoints into the run directory beside their workspace and
    run task-provided builds, tests and evaluators, so every scope is writable.
    """

    _permission = Permission(
        local=PermissionKind.ALL,
        user=PermissionKind.ALL,
        system=PermissionKind.ALL,
        online=PermissionKind.ALL,
    )
    _skills = (SKILL,)


class Agents(AgentCollection):
    coordinator: Actor
    lane_1_actor_a: Actor
    lane_1_actor_b: Actor
    lane_2_actor_a: Actor
    lane_2_actor_b: Actor
    lane_3_actor_a: Actor
    lane_3_actor_b: Actor
    human: Outworlder


class Workspace(
    LocalEnv,
    ShellEnvMixin,
    FilesEnvMixin,
    TemporaryClonedDirEnvMixin,
    ScratchDirEnvMixin,
):
    """The source workspace: Lane 1's, and what every snapshot and run directory comes from."""


class Envs(EnvCollection):
    workspace: Workspace


class PlanAgents(AgentCollection):
    coordinator: Actor


class TurnAgents(AgentCollection):
    actor: Actor


class TurnEnvs(EnvCollection):
    place: Env


class Params(FlowParams):
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
