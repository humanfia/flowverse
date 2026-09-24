"""Goal (flowbench: goal) -- the task set once as the agent's own goal."""

from typing import Annotated, NamedTuple

from hmz.flows import Agent, Allowance, Goal, flow


class Agents(NamedTuple):
    worker: Annotated[Agent, Goal]


@flow(budget=Allowance(tokens=10.0))
def run(agents: Agents, task: str) -> None:
    agents.worker.pursue(task)
