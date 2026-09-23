"""A fresh-session Ralph loop whose periodic cleanup is performed by an agent.

    hmz exec -f ralph_loop_agent_cleanup -a claude -a claude \\
        -c cleanup.yaml "improve the project"

The first configured agent gets a fresh session for every Ralph turn. A turn that
answered counts, and so does one the clock ended; a turn that answered nothing is taken
again, and three of those in a row end the run. After every cleanup_turns counted turns
the second configured agent gets a fresh cleaning session in the same workspace, run
exactly as flame_chase_agent_cleanup runs its cleaner: the tree is saved aside, the
cleaner decides what task work is worth keeping, the flow measures and repairs, runs the
optional check, replaces the history with one commit and archives the history it
replaced in the workspace's shared history.git, large files left out. An interrupted
epoch puts the tree back.

What ends the run is its allowance, which is humanize's; this flow declares ten million
output tokens by default, and `budget:` in the file passed with -c says otherwise.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from _workspace_cleanup import Config, drive, required
from hmz.flows import Agent, Allowance, Person, flow

FLOW_NAME = "ralph_loop_agent_cleanup"


class Agents(NamedTuple):
    """The Ralph coder, the cleaner, and whoever runs it."""

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
