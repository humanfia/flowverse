"""Two agents flame-chase a repository task while a cleaner distills the workspace between them."""

from __future__ import annotations

from _flame_chase_agent_cleanup import Config, Worker, Workspace, drive
from hmz.flows import AgentCollection, EnvCollection, FlowContext, Outworlder, flow


class Agents(AgentCollection):
    first_chaser: Worker
    second_chaser: Worker
    cleaner: Worker
    human: Outworlder


class Envs(EnvCollection):
    workspace: Workspace


@flow(agents=Agents, envs=Envs, params=Config, description=__doc__, resumable=True)
async def flame_chase_agent_cleanup(
    task: str, *, agents: Agents, envs: Envs, params: Config, ctx: FlowContext
) -> None:
    await drive(
        "flame_chase_agent_cleanup",
        (agents["first_chaser"], agents["second_chaser"]),
        agents["cleaner"],
        agents["human"],
        task,
        params,
        ctx,
        envs["workspace"],
    )
