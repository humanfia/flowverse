"""Ralph loop (flowbench: ralph_loop) -- a fresh session every turn, so nothing carries over.

hmz exec -f ralph_loop -a claude/MODEL:high "$(cat TASK.md)"

Add `-c budget.yaml` with a `budget:` in it to hold this run to something other than what the
flow comes with; at the prompt, `/flow` asks the same thing on the budget row of the sheet.

Nothing carries over inside a run, and one thing carries between runs: which round it is on,
kept as `rounds`. A loop like this is left going for days and is stopped -- esc, a machine
that goes down, a turn that takes the process with it -- so running it again goes on from the
round it reached rather than back at one. What the agent did is not kept: every round is a
session of its own, written down by the backend that ran it, and the next round starts from
the task and the repository whether or not it is the first.

What ends it is the run's allowance, which is humanize's and not this flow's. A loop with
nothing else to stop it runs until somebody stops it -- a bill nobody agreed to and a week of
rounds nobody read -- so every session of every agent is held to the hours, the millions of
output tokens and the dollars the run was given, and a turn taken once that is spent stops the
run rather than answering. This flow declares ten million output tokens as what it is worth by
default, which is what it has always come with; whoever runs it says otherwise.

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
    stalled = 0
    while True:
        # Said before the turn rather than counted after it, so that a run watched from the
        # outside says which round the one going now is.
        kept["rounds"] = kept.get("rounds", 0) + 1
        print(f"round {kept['rounds']}")
        # A session of its own each turn: the agent starts from the task and the repository,
        # with nothing of the last turn in context. The turn is also where the run's
        # allowance is read: a round taken once it is spent raises rather than answering,
        # which is what ends this loop and is why it needs no exit of its own.
        answered = agent(task, suppress=True)
        stalled = 0 if answered else stalled + 1
        if stalled >= STALLED:
            print(f"stopping: {stalled} rounds in a row answered with nothing")
            # Kept rather than cleared: this is a loop that was stopped rather than one that
            # is over, and what stopped it is a thing to fix and carry on from.
            return
        time.sleep(5)
