"""Flame chase (flowbench: flame_chase) -- two agents take turns on the same task.

hmz exec -f official/flame_chase \
    -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max "$(cat TASK.md)"
"""

import time
from typing import Annotated

from hmz.agents import AgentBase, AgentDefaults
from hmz.flows import flow


@flow
def run(
    agents: tuple[
        Annotated[AgentBase, AgentDefaults(goals=False)],
        Annotated[AgentBase, AgentDefaults(goals=False)],
    ],
    task: str,
) -> None:
    while True:
        for agent in agents:
            agent(task, suppress=True)  # each agent reads the repository, not a history
            time.sleep(5)
