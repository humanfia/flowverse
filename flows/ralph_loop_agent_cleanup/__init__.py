"""A fresh-session Ralph loop whose periodic cleanup is performed by an agent."""

from __future__ import annotations

from typing import Any, NamedTuple

from _ralph_loop_agent_cleanup import Config, drive, required
from hmz.flows import Agent, Allowance, Person, flow

FLOW_NAME = "ralph_loop_agent_cleanup"


class Agents(NamedTuple):
    agent: Agent
    cleaner: Agent
    human: Person


@flow(budget=Allowance(tokens=10.0), resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    drive(
        FLOW_NAME,
        (agents.agent,),
        agents.cleaner,
        agents.human,
        task,
        required(config, FLOW_NAME),
        state if state is not None else {},
    )
