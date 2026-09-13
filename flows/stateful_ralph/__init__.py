"""Stateful ralph (flowbench: stateful_ralph) -- one session, re-sent the task every turn.

hmz exec -f stateful_ralph -a kimi/PROVIDER/MODEL:high "$(cat TASK.md)"

Add `-c budget.yaml` with a `budget:` in it to hold this run to something other than what the
flow comes with; at the prompt, `/flow` asks the same thing on the budget row of the sheet.

The session is what this flow is, and it is the one thing a run picked up again cannot have
back: a session is opened rather than reopened, so running this again is a conversation of its
own, starting from the task and the repository with none of the rounds before it in context.
What does carry is which round it is on, kept as `rounds` -- so a loop stopped on its fortieth
round says round 41 when it is started again, and remembers nothing else about the forty.

What ends it is the run's allowance, which is humanize's and not this flow's. A loop with
nothing else to stop it runs until somebody stops it -- a bill nobody agreed to and a week of
rounds nobody read -- so every session of every agent is held to the hours, the millions of
output tokens and the dollars the run was given, and a turn taken once that is spent stops the
run rather than answering. This flow declares ten million output tokens as what it is worth by
default, which is what it has always come with; whoever runs it says otherwise. Output rather
than every kind, because output is what the model is asked to produce and the only kind a loop
of its own accord grows -- and here what goes in grows too, one session being one conversation
that gets longer, which is the context window's business rather than an allowance's.

Or a run of rounds that did nothing. A round whose turn failed answers with nothing and spends
nothing, so a loop whose account was refused or whose model it may not run sits under a token
allowance that never moves and goes round on the same failure for as long as it is left --
which is exactly why the allowance has a dimension in hours as well. Three such rounds in a
row end it sooner. What it kept is left rather than cleared: a loop that stalled is one to fix
and start again from, not one that is over.
"""

import time
from typing import Any

from hmz.flows import Agent, Allowance, flow

#: How many rounds in a row may answer with nothing before the loop gives up. A round that
#: failed answers with nothing under `suppress` and spends no output tokens, so a token
#: allowance meant to end the loop never moves for it. Three rather than one, because a round
#: that genuinely had nothing to say is a round like any other and not a reason to stop.
STALLED = 3


@flow(budget=Allowance(tokens=10.0), resumable=True)
def run(
    agents: tuple[Agent],
    task: str,
    state: dict[str, Any] | None = None,
) -> None:
    (agent,) = agents
    kept = state if state is not None else {}
    session = agent.new()  # one session, held for as long as the flow runs
    stalled = 0
    while True:
        kept["rounds"] = kept.get("rounds", 0) + 1
        print(f"round {kept['rounds']}")
        # Where the run's allowance is read as well as where the work is done: a round taken
        # once it is spent raises rather than answering, which is what ends this loop.
        answered = session(task, suppress=True)
        stalled = 0 if answered else stalled + 1
        if stalled >= STALLED:
            print(f"stopping: {stalled} rounds in a row answered with nothing")
            # Kept rather than cleared: this is a loop that was stopped rather than one that
            # is over, and what stopped it is a thing to fix and carry on from.
            return
        time.sleep(5)
