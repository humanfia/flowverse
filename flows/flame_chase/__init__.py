"""Flame chase (flowbench: flame_chase) -- two agents take turns on the same task."""

import time
from typing import Any

from hmz.flows import Agent, Allowance, flow


@flow(budget=Allowance(tokens=33.550336), resumable=True)
def run(
    agents: tuple[Agent, Agent],
    task: str,
    state: dict[str, Any] | None = None,
) -> None:
    kept = state if state is not None else {}
    at = kept.get("turn", 0) % len(agents)
    while True:
        agents[at](task, suppress=True)
        at = (at + 1) % len(agents)
        written: dict[str, Any] = {"turn": at}
        if at == 0:
            written["rounds"] = kept.get("rounds", 0) + 1
        kept.update(written)
        time.sleep(5)
