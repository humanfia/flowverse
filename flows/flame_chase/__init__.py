"""Flame chase (flowbench: flame_chase) -- two agents take turns on the same task.

hmz exec -f official/flame_chase \
    -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max "$(cat TASK.md)"

Add `-c budget.yaml` to hold it to something other than the budget it comes with, and
`hmz -f official/flame_chase -c budget.yaml` opens the interface on the same setup.

A run of this can be picked up where the last one left off, and what it keeps is whose turn is
next, as `turn`, how many rounds the pair have behind them, as `rounds`, and what the two of
them have spent, as `output`. The turn is the half of it that has to be kept: a run that always
opened at the first agent would hand it the turn the other one was owed, and two turns in a row
is the one thing a flow whose whole shape is two agents alternating must not do. What either of
them did is not kept: every turn is a session of its own, logged by the backend that ran it,
and an agent arriving reads the repository rather than a history.

What ends it is the budget. A loop with nothing else to stop it runs until somebody stops it,
which is a bill nobody agreed to and a week of rounds nobody read; so the pair are held to
`budget` million output tokens between them, and 0 is the loop that goes on until it is
stopped by hand. Between them rather than apiece, because the loop is the two of them: a pair
that alternates spends what it spends whichever of them was writing at the time. Output rather
than every kind, because output is what a model is asked to produce and the only kind a loop
of its own accord grows: what goes in is the task and the repository, and a turn that read
more of them is not a turn that did more.

The spend is kept because the rounds are. A budget that started again at nothing every time
the loop was picked up would be no budget at all for the loop a week of restarts is, so what
is counted is every run of this flow in this workspace. A loop that has spent it is over, and
what is over is not picked up: it clears what it kept, so the next run here opens on a budget
of its own, at round one and at the first agent, rather than stopping before it has taken a
turn.
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
        description="millions of output tokens the two may spend between them before the "
        "loop stops, counted across every run of it in this workspace, or 0 to go on until "
        "it is stopped",
    )


@flow(resumable=True)
def run(
    agents: tuple[Agent, Agent],
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    held = config or Config()
    kept = state if state is not None else {}
    # What the runs before this one spent, which this run's own is added to: an agent counts
    # what it has spent since it was made, and the loop is older than either of them.
    before = kept.get("output", 0.0)
    at = kept.get("turn", 0) % len(agents)
    while True:
        agents[at](task, suppress=True)  # which reads the repository, not a history
        at = (at + 1) % len(agents)
        # Both agents, because the loop is the pair: what it has cost is what the two of them
        # have written between them, whichever of them was taking the turn.
        spent = before + sum(one.spent().output for one in agents)
        written: dict[str, Any] = {"turn": at, "output": spent}
        # A round is a turn each, so it is the turn that finishes one that counts it rather
        # than the turn that opens one: a round the first agent was cut off in is finished by
        # the run that picks that turn up, and a round finished once is counted once.
        if at == 0:
            written["rounds"] = kept.get("rounds", 0) + 1
        # Written once the turn is over rather than before it, and in the one call: a turn
        # cut short -- the machine went down under it -- is taken again by the agent whose it
        # was, and what a run leaves says one thing about the round it stopped in.
        kept.update(written)
        if held.budget and spent >= held.budget * MILLION:
            print(f"stopping: {spent / MILLION:.2f}M output tokens of {held.budget:g}M")
            # Emptied rather than left, which is what the next run here is handed and reads
            # as a run to start clean rather than as a run to carry on and stop at once.
            kept.clear()
            return
        time.sleep(5)
