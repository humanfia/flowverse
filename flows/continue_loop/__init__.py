"""Continue loop (flowbench: continue_loop) -- send the task once, then keep nudging "continue"."""

import time
from typing import Any

from hmz.flows import Agent, Allowance, flow


@flow(budget=Allowance(tokens=10.0), resumable=True)
def run(
    agents: tuple[Agent],
    task: str,
    state: dict[str, Any] | None = None,
) -> None:
    (agent,) = agents
    kept = state if state is not None else {}
    session = agent.new()
    prompt = task
    while True:
        kept["rounds"] = kept.get("rounds", 0) + 1
        answered = session(prompt, suppress=True)
        if answered:
            prompt = "continue"
        time.sleep(5)
