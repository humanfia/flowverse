"""A fresh-session Ralph loop whose periodic cleanup is performed by an agent."""

from __future__ import annotations

from _ralph_loop_agent_cleanup import Config, Worker, Workspace, drive
from hmz.flows import AgentCollection, EnvCollection, FlowContext, Outworlder, flow


class Agents(AgentCollection):
    agent: Worker
    cleaner: Worker
    human: Outworlder


class Envs(EnvCollection):
    workspace: Workspace


@flow(agents=Agents, envs=Envs, params=Config, description=__doc__, resumable=True)
async def ralph_loop_agent_cleanup(
    task: str, *, agents: Agents, envs: Envs, params: Config, ctx: FlowContext
) -> None:
    await drive(
        "ralph_loop_agent_cleanup",
        (agents["agent"],),
        agents["cleaner"],
        agents["human"],
        task,
        params,
        ctx,
        envs["workspace"],
    )
