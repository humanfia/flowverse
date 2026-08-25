"""Continue loop (flowbench: continue_loop) -- send the task once, then keep nudging "continue".

hmz exec -f official/continue_loop -a kimi/kimi-code/k3:high "$(cat TASK.md)"

Add `-c budget.yaml` to hold it to something other than the budget it comes with, and
`hmz -f official/continue_loop -c budget.yaml` opens the interface on the same setup.

A run of this can be picked up where the last one left off, and what it keeps is the round it
is on, as `rounds` -- counted as a round begins, so a run cut off inside one starts the next
round rather than taking that one again -- and what the loop has spent, as `output`. That the
task has been sent is not kept: "continue" means something only to the session that heard the
task, no backend reopens a named session, and so a picked-up run opens one that has heard
nothing and starts it on the task exactly as the first run did. What the agent went on to say
is the backend's own log to keep, not this flow's.

What ends it is the budget. A loop with nothing else to stop it runs until somebody stops it,
which is a bill nobody agreed to and a week of rounds nobody read; so it is held to `budget`
million output tokens, and 0 is the loop that goes on until it is stopped by hand. Output
rather than every kind, because output is what the model is asked to produce and the only
kind a loop of its own accord grows: what goes in is the task and the repository, and a round
that read more of them is not a round that did more.

The spend is kept because the rounds are. A budget that started again at nothing every time
the loop was picked up would be no budget at all for the loop a week of restarts is, so what
is counted is every run of this flow in this workspace. A loop that has spent it is over, and
what is over is not picked up: it clears what it kept, so the next run here opens on a budget
of its own and at round one rather than stopping before it has taken a turn.
"""

import time
from typing import Any

from hmz.flows import Agent, flow
from pydantic import BaseModel, Field

#: Output tokens in one of the millions a budget is written in. The budget is written that
#: way because that is the size these loops come in: a round of one is thousands, and a day
#: of rounds is millions.
MILLION = 1_000_000.0


class Config(BaseModel):
    """What this flow takes."""

    model_config = {"extra": "forbid"}

    budget: float = Field(
        default=10.0,
        ge=0,
        description="millions of output tokens the loop may spend before it stops, counted "
        "across every run of it in this workspace, or 0 to go on until it is stopped",
    )


@flow(resumable=True)
def run(
    agents: tuple[Agent],
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    (agent,) = agents
    held = config or Config()
    kept = state if state is not None else {}
    # What the runs before this one spent, which this run's own is added to: an agent counts
    # what it has spent since it was made, and the loop is older than any of them.
    before = kept.get("output", 0.0)
    session = agent.new()
    # The task, whatever run this is picking up from: the session is new either way, and a
    # session nudged to continue work nobody told it about continues nothing.
    prompt = task
    while True:
        # Counted where the round begins, so that the number is the round going on now.
        kept["rounds"] = kept.get("rounds", 0) + 1
        answered = session(prompt, suppress=True)
        # Until a turn lands, the task is sent again: "continue" on its own would open a
        # session that never saw it.
        if answered:
            prompt = "continue"
        kept["output"] = spent = before + agent.spent().output
        if held.budget and spent >= held.budget * MILLION:
            print(f"stopping: {spent / MILLION:.2f}M output tokens of {held.budget:g}M")
            # Emptied rather than left, which is what the next run here is handed and reads
            # as a run to start clean rather than as a run to carry on and stop at once.
            kept.clear()
            return
        time.sleep(5)
