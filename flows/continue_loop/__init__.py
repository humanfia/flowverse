"""Continue loop (flowbench: continue_loop) -- send the task once, then keep nudging "continue".

hmz exec -f official/continue_loop -a kimi/kimi-code/k3:high "$(cat TASK.md)"

Add `-c budget.yaml` with a `budget:` in it to hold this run to something other than what the
flow comes with, and `hmz -f official/continue_loop -c budget.yaml` opens the interface on the
same setup.

A run of this can be picked up where the last one left off, and what it keeps is the round it
is on, as `rounds` -- counted as a round begins, so a run cut off inside one starts the next
round rather than taking that one again. That the task has been sent is not kept: "continue"
means something only to the session that heard the task, no backend reopens a named session,
and so a picked-up run opens one that has heard nothing and starts it on the task exactly as
the first run did. What the agent went on to say is the backend's own log to keep, not this
flow's.

What ends it is the run's allowance, which is humanize's and not this flow's. A loop with
nothing else to stop it runs until somebody stops it -- a bill nobody agreed to and a week of
rounds nobody read -- so every session of every agent is held to the hours, the millions of
output tokens and the dollars the run was given, and a turn taken once that is spent stops the
run rather than answering. This flow declares ten million output tokens as what it is worth by
default, which is what it has always come with; whoever runs it says otherwise. Output rather
than every kind, because output is what the model is asked to produce and the only kind a loop
of its own accord grows: what goes in is the task and the repository, and a round that read
more of them is not a round that did more.
"""

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
    # The task, whatever run this is picking up from: the session is new either way, and a
    # session nudged to continue work nobody told it about continues nothing.
    prompt = task
    while True:
        # Counted where the round begins, so that the number is the round going on now.
        kept["rounds"] = kept.get("rounds", 0) + 1
        # And where the run's allowance is read: a round taken once it is spent raises rather
        # than answering, which is what ends this loop and is why it needs no exit of its own.
        answered = session(prompt, suppress=True)
        # Until a turn lands, the task is sent again: "continue" on its own would open a
        # session that never saw it.
        if answered:
            prompt = "continue"
        time.sleep(5)
