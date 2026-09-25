"""Run fixed Git PR Lite with Report Share and deterministic integration."""

from __future__ import annotations

from typing import Literal

from _parallel_flame_chase_git_pr.models import InitialPlan, LaneReport
from _parallel_flame_chase_git_pr.prompts import lane_repair_prompt
from _parallel_flame_chase_git_pr.runtime import SKILL, GitPRRuntime
from hmz.flows import (
    Agent,
    AgentCollection,
    Env,
    EnvCollection,
    FilesEnvMixin,
    FlowContext,
    FlowParams,
    LocalEnv,
    Outworlder,
    Permission,
    PermissionKind,
    ScratchDirEnvMixin,
    ShellEnvMixin,
    TemporaryClonedDirEnvMixin,
    flow,
)
from pydantic import Field

REPAIR_ATTEMPTS = 3


class Orchestrator(Agent):
    _skills = (SKILL,)


class LaneActor(Agent):
    # A lane pushes to the run's central repository and records receipts beside it, both
    # outside its own clone.
    _permission = Permission(user=PermissionKind.ALL)
    _skills = (SKILL,)


class Workspace(
    LocalEnv,
    ShellEnvMixin,
    FilesEnvMixin,
    ScratchDirEnvMixin,
    TemporaryClonedDirEnvMixin,
): ...


class Agents(AgentCollection):
    orchestrator: Orchestrator
    lane_1_actor_a: LaneActor
    lane_1_actor_b: LaneActor
    lane_2_actor_a: LaneActor
    lane_2_actor_b: LaneActor
    lane_3_actor_a: LaneActor
    lane_3_actor_b: LaneActor
    human: Outworlder


class Envs(EnvCollection):
    workspace: Workspace


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
        default=5_000,
        ge=1,
        le=100_000_000,
        description="Regular-file count that marks a source workspace as oversized.",
    )
    workspace_copy_warning_threshold_bytes: int = Field(
        default=1024**3,
        ge=1,
        le=1_000_000_000_000_000,
        description=(
            "Estimated bytes across new workspace materializations that trigger a warning."
        ),
    )
    git_pr_enabled: Literal[True] = True
    global_knowledge_enabled: Literal[False] = False
    experiment_memory_enabled: Literal[False] = False
    token_efficient_enabled: Literal[False] = False
    main_update_monitor_enabled: Literal[False] = False


class PlanningAgents(AgentCollection):
    orchestrator: Orchestrator


class ActorAgents(AgentCollection):
    actor: LaneActor


class TurnEnvs(EnvCollection):
    workdir: Env


@flow(agents=PlanningAgents, envs=TurnEnvs, params=FlowParams, name="plan", hidden=True)
async def plan(
    task: str,
    *,
    agents: PlanningAgents,
    envs: TurnEnvs,
    params: FlowParams,
    ctx: FlowContext,
) -> InitialPlan:
    """Plan the three lanes in one fresh orchestrator session."""
    orchestrator = agents["orchestrator"]
    session = await orchestrator.spawn(env=envs["workdir"])
    return await orchestrator.run(task, session=session, output_schema=InitialPlan)


@flow(
    agents=ActorAgents, envs=TurnEnvs, params=FlowParams, name="lane-turn", hidden=True
)
async def lane_turn(
    task: str,
    *,
    agents: ActorAgents,
    envs: TurnEnvs,
    params: FlowParams,
    ctx: FlowContext,
) -> LaneReport:
    """Take one lane turn in a fresh session, repairing a malformed LaneReport."""
    actor = agents["actor"]
    session = await actor.spawn(env=envs["workdir"])
    prompt = task
    for _ in range(REPAIR_ATTEMPTS - 1):
        try:
            return await actor.run(prompt, session=session, output_schema=LaneReport)
        except ValueError as why:
            prompt = lane_repair_prompt(f"{type(why).__name__}: {why}"[:2000])
    return await actor.run(prompt, session=session, output_schema=LaneReport)


@flow(agents=Agents, envs=Envs, params=Params, resumable=True)
async def parallel_flame_chase_git_pr(
    task: str,
    *,
    agents: Agents,
    envs: Envs,
    params: Params,
    ctx: FlowContext,
) -> None:
    """Run fixed Git PR Lite with Report Share and deterministic integration."""
    await GitPRRuntime(
        task,
        agents=agents,
        envs=envs,
        params=params,
        ctx=ctx,
        plan=plan,
        lane_turn=lane_turn,
    ).run()


__all__ = [
    "Agents",
    "Envs",
    "Params",
    "lane_turn",
    "parallel_flame_chase_git_pr",
    "plan",
]
