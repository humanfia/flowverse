"""Continue loop (flowbench: continue_loop) -- send the task once, then keep nudging "continue".

hmz exec -f official/continue_loop -a kimi/kimi-code/k3:high "$(cat TASK.md)"

A run of this can be picked up where the last one left off, and what it keeps is the round it
is on, as `rounds` -- counted as a round begins, so a run cut off inside one starts the next
round rather than taking that one again. That the task has been sent is not kept: "continue"
means something only to the session that heard the task, no backend reopens a named session,
and so a picked-up run opens one that has heard nothing and starts it on the task exactly as
the first run did. What the agent went on to say is the backend's own log to keep, not this
flow's.
"""

import time
from typing import Any

from hmz.agents import AgentBase
from hmz.flows import flow


@flow(resumable=True)
def run(agents: tuple[AgentBase], task: str, state: dict[str, Any]) -> None:
    (agent,) = agents
    session = agent.new()
    # The task, whatever run this is picking up from: the session is new either way, and a
    # session nudged to continue work nobody told it about continues nothing.
    prompt = task
    while True:
        # Counted where the round begins, so that the number is the round going on now.
        state["rounds"] = state.get("rounds", 0) + 1
        answered = session(prompt, suppress=True)
        # Until a turn lands, the task is sent again: "continue" on its own would open a
        # session that never saw it.
        if answered:
            prompt = "continue"
        time.sleep(5)
