"""Fixed-juice ralph (flowbench: fixed_juice_ralph) -- a ralph loop held to an answer size.

    hmz exec -f official/fixed_juice_ralph -a claude/claude-opus-4-8:high "$(cat TASK.md)"

Add `-c juice.yaml` to say how much juice to hold it to rather than take the one it comes
with, and `hmz -f official/fixed_juice_ralph -c juice.yaml` opens the interface on the same
setup.

The ralph loop with a governor on it: a fresh session every turn, and between the turns the
effort moved to hold the agent to `juice` output tokens per turn of the model.

A governor is not a brake. What it holds steady is the size of an answer, so a loop held to
2000 output tokens a turn is a loop that goes on producing 2000 output tokens a turn for as
long as anybody leaves it running. What ends it is `budget`, which is how many millions of
output tokens the whole of it may come to, and 0 is the loop that goes on until it is stopped
by hand. The two are one quantity read at two scales: `juice` is what a turn is worth, and
`budget` is what the loop is.

Per turn of the *model* -- one request and the answer to it -- rather than per turn of the
flow, which is many of those and as many again of whatever the tools took. That average is
what an effort actually moves: a model asked to think harder writes more in each answer and
takes longer over it. A model asked to think less writes less. So an agent under the target is
asked to think harder and one over it is asked to think less, one rung of its own model's
ladder per round, so that the loop settles rather than swings.

Nothing here is a clock. How long a round takes and what it costs an hour are what the model
and the work make of it; what this holds steady is how much of an answer each turn is worth.

It can be picked up where the last run of it left off, and what is worth picking up is where
the governor got to: it keeps the rung it settled at, as `effort`, which round it is on, as
`rounds`, and what it has spent, as `output` -- a budget that started again at nothing every
time the loop was picked up being no budget at all for the loop a week of restarts is. A loop started again from the middle of the ladder walks back up to that rung a
round at a time, and every one of those rounds is a turn of a model somebody is paying for.
The rung is kept as the effort's own word rather than as a place on the ladder, because the
ladder is whatever the account says its CLI runs today: a model retired and an effort added
both move the places, and a word that is no longer on the ladder at all is read as no rung
and leaves the agent where a first run would start it. The answer size is not kept -- it is an
average over the last few minutes of turns, and a run starting today has none of yesterday's
minutes to average, so the first round of it measures its own.

A loop that has spent its budget is over, and what is over is not picked up: it clears what it
kept, so the next run here opens on a budget of its own, at round one and at the rung the
agent was configured with, rather than stopping before it has taken a turn.

Which is a flow rather than a setting because it is a policy: how much thinking a job is worth
is the sort of thing that changes between projects, and this is one answer to it written down.
"""

import time
from typing import Any

from hmz.flows import SWARM, Agent, flow, models
from pydantic import BaseModel, Field

#: Output tokens in one of the millions a budget is written in. The budget is written that
#: way because that is the size these loops come in: a round of one is thousands, and a day
#: of rounds is millions.
MILLION = 1_000_000.0


class Config(BaseModel):
    """What this flow takes."""

    model_config = {"extra": "forbid"}

    juice: float = Field(
        default=2000.0,
        gt=0,
        description="output tokens an average turn of the model is to come out with, which "
        "is what the effort is moved to hold it to",
    )
    over: float = Field(
        default=300.0,
        ge=10,
        le=3600,
        description="how far back that average is taken, in seconds",
    )
    slack: float = Field(
        default=0.15,
        ge=0,
        le=1,
        description="how far off the average may be before the effort moves, as a fraction "
        "of the target -- 0.15 leaves it alone between 85% and 115% of it",
    )
    rest: float = Field(
        default=5.0,
        ge=0,
        le=600,
        description="how long to wait between rounds, in seconds",
    )
    budget: float = Field(
        default=10.0,
        ge=0,
        description="millions of output tokens the loop may spend before it stops, counted "
        "across every run of it in this workspace, or 0 to go on until it is stopped",
    )


def ladder(agent: Agent) -> tuple[str, ...]:
    """The efforts this agent's model takes, hardest first.

    Read out of what that CLI last said it runs as this agent's account, which is where every
    other reader of it looks: nothing is written down about a model here or anywhere else in
    humanize, since a list of them is wrong the day a CLI ships one. A model the account has
    never been asked about -- one nobody has run a `hmz providers` against, one that arrived
    since -- is offered the first ladder that account did name, every model of a backend
    taking the same efforts unless that backend says otherwise; an account that has said
    nothing at all leaves the agent at what it was configured with, which is a loop with
    nothing to turn.

    Args:
      agent: The agent whose model it is.

    Returns:
      One effort per rung, hardest first, or just the configured one where none is known --
      the thinking of it, since a width written in front of that is not a rung and rides
      along with whichever one the loop is on.
    """
    offered = models.offered(agent.backend, agent.config.provider)
    if not offered:
        return (agent.config.effort.removeprefix(SWARM),)
    named = agent.config.model
    for model in offered:
        if model.name == named:
            return model.efforts
    return offered[0].efforts


def _at(agent: Agent, rungs: tuple[str, ...], settled: str = "") -> int:
    """Which rung the loop starts on: the one it settled at last, or the agent's own.

    The rung a run before this one settled at is what this run has to steer from, and it is
    read here as a word rather than trusted as a place: the ladder is read off what that
    account says its CLI runs, which is an answer that moves, and a word it no longer holds
    is a rung this model does not have. Then the agent is placed as it would be on a first
    run -- on the effort it was configured with, or on the middle rung where the ladder does
    not hold that one either.

    Kimi's effort says how wide to run as well as how hard, and the width goes with it: the
    rung is the thinking, and the prefix rides along.

    Args:
      agent: The agent to place.
      rungs: The ladder, hardest first.
      settled: The rung the last run of this flow settled at, or "" for a first run.

    Returns:
      The index of the rung it starts on.
    """
    for said in (settled, agent.effort.removeprefix(SWARM)):
        if said in rungs:
            return rungs.index(said)
    return len(rungs) // 2


@flow(resumable=True)
def run(
    agents: tuple[Agent],
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Runs the loop, holding the agent to the answer size it was set up with.

    Args:
      agents: The one agent it drives.
      task: What it is to do, every turn, from the repository and nothing else.
      config: How much juice to hold it to and how to hold it, or None for the defaults.
      state: What the last run of it settled at, and what this one writes its own into, or
        None for a call from outside a run -- which is a loop that governs itself as ever
        and leaves nothing behind.
    """
    (agent,) = agents
    held = config or Config()
    kept = state if state is not None else {}
    # What the runs before this one spent, which this run's own is added to: an agent counts
    # what it has spent since it was made, and the loop is older than any of them.
    before = kept.get("output", 0.0)
    rungs = ladder(agent)
    wide = SWARM if agent.effort.startswith(SWARM) else ""
    at = _at(agent, rungs, kept.get("effort", ""))
    while True:
        # Said before the turn rather than counted after it, so that a run watched from the
        # outside says which round the one going now is.
        kept["rounds"] = kept.get("rounds", 0) + 1
        # And the rung asked for before the turn rather than after it, so that the first turn
        # of a run picked up is taken at the rung the run before it settled at: an effort set
        # only once a turn has been taken would have that turn steer the loop from a rung
        # nobody meant it to be on.
        agent.effort = f"{wide}{rungs[at]}"
        # A session of its own each round: the agent starts from the task and the repository,
        # with nothing of the last round in context. What carries over is the effort.
        agent(task, suppress=True)
        # What its turns of the model have been coming out with lately. A round that landed
        # no turn at all -- one whose backend failed before it said anything -- leaves nothing
        # to steer by, and the effort is left where it was rather than moved on a zero.
        juice = agent.juice(over=held.over)
        if juice and juice < held.juice * (1 - held.slack):
            at = max(at - 1, 0)  # thin answers: think harder, and write more in each
        elif juice > held.juice * (1 + held.slack):
            at = min(at + 1, len(rungs) - 1)  # long ones: think less, and answer sooner
        # Where the governor has got to, which is what the next run of this starts from. The
        # answer size that moved it is not kept: it is measured over the last few minutes of
        # turns, and a run that has not taken any has no minutes to read it over.
        kept["effort"] = rungs[at]
        kept["output"] = spent = before + agent.spent().output
        # The budget said beside the round only where there is one: a loop told to go on
        # until it is stopped has no fraction of anything to report.
        of = f" · {spent / MILLION:.2f}M of {held.budget:g}M" if held.budget else ""
        print(
            f"round {kept['rounds']} · {juice:.0f}/{held.juice:g} out per turn · "
            f"{wide}{rungs[at]}{of}"
        )
        if held.budget and spent >= held.budget * MILLION:
            print(f"stopping: {spent / MILLION:.2f}M output tokens of {held.budget:g}M")
            # Emptied rather than left, which is what the next run here is handed and reads
            # as a run to start clean rather than as a run to carry on and stop at once.
            kept.clear()
            return
        time.sleep(held.rest)
