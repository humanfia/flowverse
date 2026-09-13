"""Goal loop (flowbench: goal) -- ralph, with the task set as the agent's own goal.

hmz exec -f official/goal -a claude/claude-opus-4-8:max "$(cat TASK.md)"

Add `-c budget.yaml` with a `budget:` in it to hold this run to something other than what the
flow comes with, and `hmz -f official/goal -c budget.yaml` opens the interface on the same
setup.

A run of this can be picked up where the last one left off, and what it keeps is which round
it is on, as `rounds` -- counted as a round begins, so a run cut off inside one starts the
next round rather than taking that one again. There is nothing else it could honestly keep: a
goal is pursued in a session of its own and nothing of it carries into the next one, so a
round begun by a run picked up starts from the task and the repository exactly as the first
round of the first run did.

What ends it is the run's allowance, which is humanize's and not this flow's. A loop with
nothing else to stop it runs until somebody stops it -- a bill nobody agreed to and a week of
rounds nobody read -- so every session of every agent is held to the hours, the millions of
output tokens and the dollars the run was given, and a turn taken once that is spent stops the
run rather than answering. This flow declares ten million output tokens as what it is worth by
default, which is what it has always come with; whoever runs it says otherwise.

Which is the one thing that bounds this loop at all: a goal is the agent deciding for itself
when it has finished, and the loop is what starts it over each time it decides that and was
wrong. A goal is read at the edges of the session it runs in rather than inside it, so a goal
that burns for an hour in one call is not cut off mid-call: it stops at the next round.
"""

import time
from typing import Annotated, Any, NamedTuple

from hmz.flows import Agent, Allowance, Goal, flow


class Agents(NamedTuple):
    """The one this drives, which is run under a goal rather than by turns."""

    worker: Annotated[Agent, Goal]


@flow(budget=Allowance(tokens=10.0), resumable=True)
def run(
    agents: Agents,
    task: str,
    state: dict[str, Any] | None = None,
) -> None:
    (agent,) = agents
    kept = state if state is not None else {}
    while True:
        # Counted where the round begins, so that the number is the round going on now.
        kept["rounds"] = kept.get("rounds", 0) + 1
        # A turn here is a goal: the agent keeps itself going until it has met the task, and
        # the loop is only what starts it over when it stopped without having. It is also
        # where the run's allowance is read -- a round begun once it is spent raises rather
        # than pursuing anything, which is what ends this loop.
        agent.pursue(task, suppress=True)
        time.sleep(5)
