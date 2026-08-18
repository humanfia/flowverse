"""Flame chase (flowbench: flame_chase) -- two agents take turns on the same task.

hmz exec -f official/flame_chase \
    -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max "$(cat TASK.md)"

A run of this can be picked up where the last one left off, and what it keeps is whose turn is
next, as `turn`, and how many rounds the pair have behind them, as `rounds`. The turn is the
half of it that has to be kept: a run that always opened at the first agent would hand it the
turn the other one was owed, and two turns in a row is the one thing a flow whose whole shape
is two agents alternating must not do. What either of them did is not kept: every turn is a
session of its own, logged by the backend that ran it, and an agent arriving reads the
repository rather than a history.
"""

import time
from typing import Any

from hmz.flows import Agent, flow


@flow(resumable=True)
def run(agents: tuple[Agent, Agent], task: str, state: dict[str, Any]) -> None:
    at = state.get("turn", 0) % len(agents)
    while True:
        agents[at](task, suppress=True)  # which reads the repository, not a history
        at = (at + 1) % len(agents)
        held = {"turn": at}
        # A round is a turn each, so it is the turn that finishes one that counts it rather
        # than the turn that opens one: a round the first agent was cut off in is finished by
        # the run that picks that turn up, and a round finished once is counted once.
        if at == 0:
            held["rounds"] = state.get("rounds", 0) + 1
        # Both written once the turn is over rather than before it, and in the one call: a
        # turn cut short -- the machine went down under it -- is taken again by the agent
        # whose it was, and what a run leaves says one thing about the round it stopped in.
        state.update(held)
        time.sleep(5)
