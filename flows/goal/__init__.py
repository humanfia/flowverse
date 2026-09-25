"""Goal (flowbench: goal) -- the task set once as the agent's own goal.

    hmz exec -f goal -a worker=claude/claude-opus-5:high -b cost=5 "the task"
"""

from hmz.flows import (
    Agent,
    AgentCollection,
    EnvCollection,
    FlowContext,
    FlowParams,
    GoalCommandAgentMixin,
    LocalEnv,
    flow,
)


class Worker(Agent, GoalCommandAgentMixin): ...


class Agents(AgentCollection):
    worker: Worker


class Envs(EnvCollection):
    workspace: LocalEnv


@flow(agents=Agents, envs=Envs, params=FlowParams)
async def goal(
    task: str, *, agents: Agents, envs: Envs, params: FlowParams, ctx: FlowContext
) -> None:
    """The task set once as the agent's own goal."""
    worker = agents["worker"]
    session = await worker.spawn(env=envs["workspace"])
    await worker.run(f"/goal {task}", session=session)
