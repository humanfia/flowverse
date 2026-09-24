"""Two agents flame-chase a repository task while a cleaner distills the workspace between them."""

from __future__ import annotations

from typing import Any, NamedTuple

from _flame_chase_agent_cleanup import Config, drive, required
from hmz.flows import Agent, Allowance, Person, flow

FLOW_NAME = "flame_chase_agent_cleanup"


class Agents(NamedTuple):
    first_chaser: Agent
    second_chaser: Agent
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
        (agents.first_chaser, agents.second_chaser),
        agents.cleaner,
        agents.human,
        task,
        required(config, FLOW_NAME),
        state if state is not None else {},
    )
