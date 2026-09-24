"""RLAR (flowbench: rlar) -- an actor works in one session, and a fresh reviewer reads its work."""

import time
from typing import Any, NamedTuple

from hmz.flows import Agent, flow
from pydantic import BaseModel, Field


class Agents(NamedTuple):
    actor: Agent
    reviewer: Agent


class Review(BaseModel):
    """What one round's review comes to: whether it is over, and what the actor is told.

    The fields are what the reviewer is asked for -- the descriptions here are the whole of
    the instruction, since they are what the backend is given as the shape to answer in.
    """

    model_config = {"extra": "forbid"}

    done: bool = Field(
        description="True only if the task is completely and correctly done: everything "
        "asked for is implemented, it works, nothing was faked, stubbed or special-cased to "
        "pass, and there is no next step worth taking. False if there is anything at all "
        "left to do or to fix."
    )
    notes: str = Field(
        description="The review itself, written as a message to the coding agent: what is "
        "done, what is wrong or missing, and what to do next, citing specific files, lines "
        "and commands. It is passed on word for word and is all the agent will hear from "
        "you, so leave nothing to be inferred. When done is true, this is what the run "
        "finishes on: say what was built and how it was checked."
    )


REVIEW_PROMPT = """You are a meticulous reviewer, running in the working directory of a coding \
agent that has been given the task below. Use shell tools (cat, ls, git status, git diff, etc.) \
to review what it has actually done against the state of the repository. Be skeptical: treat \
reward hacking -- tests weakened or special-cased, work stubbed out or faked -- as the thing you \
are most there to catch.

Task (TASK.md):
"""

PICKED_UP = """{task}

Work in this repository is already under way: an earlier run here was stopped before it \
finished, and below is a reviewer's reading of the last round of it. That run may have been on \
the task above or on something else -- what carries over is the repository, not the task. You \
did not do that work and have no record of it beyond what the files now hold, so read them \
first, then carry on with the task above, taking the review as far as it bears on it.

Review of the last round:
{notes}"""


@flow(resumable=True)
def run(agents: Agents, task: str, state: dict[str, Any]) -> None:
    working = agents.actor.new()
    notes = state.get("notes") or ""
    prompt = PICKED_UP.format(task=task, notes=notes) if notes else task
    while True:
        worked = working(prompt, suppress=True)
        if worked:
            review = agents.reviewer(REVIEW_PROMPT + task, suppress=True, schema=Review)
            if review is not None and review.done:
                print(review.notes)
                state.clear()
                return
            if review is not None and review.notes:
                prompt = notes = review.notes
            state.update(rounds=state.get("rounds", 0) + 1, notes=notes)
        time.sleep(5)
