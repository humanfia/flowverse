"""Ralph loop (flowbench: ralph_loop) -- a fresh session every turn, so nothing carries over."""

import time
from typing import Any

from hmz.flows import Agent, Allowance, flow

STALLED = 3


@flow(budget=Allowance(tokens=10.0), resumable=True)
def run(
    agents: tuple[Agent],
    task: str,
    state: dict[str, Any] | None = None,
) -> None:
    (agent,) = agents
    kept = state if state is not None else {}
    stalled = 0
    while True:
        kept["rounds"] = kept.get("rounds", 0) + 1
        print(f"round {kept['rounds']}")
        answered = agent(task, suppress=True)
        stalled = 0 if answered else stalled + 1
        if stalled >= STALLED:
            print(f"stopping: {stalled} rounds in a row answered with nothing")
            return
        time.sleep(5)
