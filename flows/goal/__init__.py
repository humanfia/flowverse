"""Goal loop (flowbench: goal) -- ralph, with the task set as the agent's own goal.

hmz exec -f official/goal -a claude/claude-opus-4-8:max "$(cat TASK.md)"

A run of this can be picked up where the last one left off, and what it keeps is which round it
is on, as `rounds` -- counted as a round begins, so a run cut off inside one starts the next
round rather than taking that one again. There is nothing else it could honestly keep: a goal
is pursued in a session of its own and nothing of it carries into the next one, so a round
begun by a run picked up starts from the task and the repository exactly as the first round of
the first run did.
"""

import time
from typing import Annotated, Any, NamedTuple

from hmz.agents import AgentBase, Goal
from hmz.flows import flow


class Agents(NamedTuple):
    """The one this drives, which is run under a goal rather than by turns."""

    worker: Annotated[AgentBase, Goal]


@flow(resumable=True)
def run(agents: Agents, task: str, state: dict[str, Any]) -> None:
    (agent,) = agents
    while True:
        # Counted where the round begins, so that the number is the round going on now.
        state["rounds"] = state.get("rounds", 0) + 1
        # A turn here is a goal: the agent keeps itself going until it has met the task, and
        # the loop is only what starts it over when it stopped without having.
        agent.pursue(task, suppress=True)
        time.sleep(5)
