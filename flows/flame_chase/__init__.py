"""Flame chase (flowbench: flame_chase) -- two agents take turns on the same task.

hmz exec -f official/flame_chase \
    -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max "$(cat TASK.md)"

Add `-c budget.yaml` with a `budget:` in it to hold this run to something other than what the
flow comes with, and `hmz -f official/flame_chase -c budget.yaml` opens the interface on the
same setup.

A run of this can be picked up where the last one left off, and what it keeps is whose turn is
next, as `turn`, and how many rounds the pair have behind them, as `rounds`. The turn is the
half of it that has to be kept: a run that always opened at the first agent would hand it the
turn the other one was owed, and two turns in a row is the one thing a flow whose whole shape
is two agents alternating must not do. What either of them did is not kept: every turn is a
session of its own, logged by the backend that ran it, and an agent arriving reads the
repository rather than a history.

What ends it is the run's allowance, which is humanize's and not this flow's. A loop with
nothing else to stop it runs until somebody stops it -- a bill nobody agreed to and a week of
rounds nobody read -- so every session of every agent is held to the hours, the millions of
output tokens and the dollars the run was given, and a turn taken once that is spent stops the
run rather than answering. This flow declares ten million output tokens as what it is worth by
default, which is what it has always come with; whoever runs it says otherwise.

Between the two of them rather than apiece, which it always was and which is now the ordinary
case rather than this flow's own arithmetic: an allowance is the run's money, and every agent
of the run spends out of the one reckoning whichever of them was writing at the time.
"""

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
        # Which reads the repository, not a history -- and which is where the run's allowance
        # is read: a turn taken once it is spent raises rather than answering, which is what
        # ends this loop and is why it needs no exit of its own.
        agents[at](task, suppress=True)
        at = (at + 1) % len(agents)
        written: dict[str, Any] = {"turn": at}
        # A round is a turn each, so it is the turn that finishes one that counts it rather
        # than the turn that opens one: a round the first agent was cut off in is finished by
        # the run that picks that turn up, and a round finished once is counted once.
        if at == 0:
            written["rounds"] = kept.get("rounds", 0) + 1
        # Written once the turn is over rather than before it, and in the one call: a turn
        # cut short -- the machine went down under it -- is taken again by the agent whose it
        # was, and what a run leaves says one thing about the round it stopped in.
        kept.update(written)
        time.sleep(5)
