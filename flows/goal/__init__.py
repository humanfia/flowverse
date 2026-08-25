"""Goal loop (flowbench: goal) -- ralph, with the task set as the agent's own goal.

hmz exec -f official/goal -a claude/claude-opus-4-8:max "$(cat TASK.md)"

Add `-c budget.yaml` to hold it to something other than the budget it comes with, and
`hmz -f official/goal -c budget.yaml` opens the interface on the same setup.

A run of this can be picked up where the last one left off, and what it keeps is which round it
is on, as `rounds` -- counted as a round begins, so a run cut off inside one starts the next
round rather than taking that one again -- and what the loop has spent, as `output`. There is
nothing else it could honestly keep: a goal is pursued in a session of its own and nothing of
it carries into the next one, so a round begun by a run picked up starts from the task and the
repository exactly as the first round of the first run did.

What ends it is the budget. A loop with nothing else to stop it runs until somebody stops it,
which is a bill nobody agreed to and a week of rounds nobody read; so it is held to `budget`
million output tokens, and 0 is the loop that goes on until it is stopped by hand. Which is
the one thing that bounds this loop at all: a goal is the agent deciding for itself when it
has finished, and the loop is what starts it over each time it decides that and was wrong.

The spend is kept because the rounds are. A budget that started again at nothing every time
the loop was picked up would be no budget at all for the loop a week of restarts is, so what
is counted is every run of this flow in this workspace. A loop that has spent it is over, and
what is over is not picked up: it clears what it kept, so the next run here opens on a budget
of its own and at round one rather than stopping before it has pursued anything.
"""

import time
from typing import Annotated, Any, NamedTuple

from hmz.flows import Agent, Goal, flow
from pydantic import BaseModel, Field

#: Output tokens in one of the millions a budget is written in. The budget is written that
#: way because that is the size these loops come in: a round of one is thousands, and a day
#: of rounds is millions.
MILLION = 1_000_000.0


class Agents(NamedTuple):
    """The one this drives, which is run under a goal rather than by turns."""

    worker: Annotated[Agent, Goal]


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
    agents: Agents,
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
    while True:
        # Counted where the round begins, so that the number is the round going on now.
        kept["rounds"] = kept.get("rounds", 0) + 1
        # A turn here is a goal: the agent keeps itself going until it has met the task, and
        # the loop is only what starts it over when it stopped without having.
        agent.pursue(task, suppress=True)
        # Every turn of the model the goal took, which is what a goal is counted as: the
        # backend started them itself, and the agent counted all of them.
        kept["output"] = spent = before + agent.spent().output
        if held.budget and spent >= held.budget * MILLION:
            print(f"stopping: {spent / MILLION:.2f}M output tokens of {held.budget:g}M")
            # Emptied rather than left, which is what the next run here is handed and reads
            # as a run to start clean rather than as a run to carry on and stop at once.
            kept.clear()
            return
        time.sleep(5)
