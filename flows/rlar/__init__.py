"""RLAR (flowbench: rlar) -- an actor works in one session, and a fresh reviewer reads its work.

    hmz exec -f official/rlar \
        -a claude/claude-opus-4-8:high -a claude/claude-opus-4-8:high "$(cat TASK.md)"

The actor must remember and the reviewer must not. Give the two the same model and effort and
they are still two agents, which is the point -- a trace reads the actor's session and the
reviewer's rounds as two.

The reviewer answers two things at once: whether the task is finished, and what to say to the
actor about it. The second is the actor's next prompt, word for word, so what the reviewer
noticed is what the actor hears; the first is what ends the loop. Both are read off the object
the reviewer is held to rather than off a marker at the end of a paragraph, so a review that
says the work is done and a review that says the words "it is done" are not the same thing.

A run of this can be picked up where the last one left off, and what it keeps is the round it
reached, as `rounds`, and the one review nobody has acted on, as `notes`. The actor's session
is not picked up with it: no backend reopens a named session, so the actor of a picked-up run
arrives having seen neither the task nor the work already in the repository, and the prompt it
opens on is the whole of what it is told. A picked-up round is therefore both -- the task, and
then the review, marked as an earlier round's reading of work this session did not do. The task
has to be in it because the state is not the task's: humanize keeps one per flow per workspace
and nothing about what the run that left it was for, so the same state is handed to whatever is
started here next, and an actor told only the review would work on the last run's task while
the reviewer marks it against this one. Which is also why the review goes in under the task as
the last run's rather than as this task's own: it may be either, and nothing here can tell.

The review is kept word for word, and this is the only place in these flows where an agent's
own prose is written into a file that outlives the run. It earns that: it is what the next
round is owed. The reviewer wrote it as the actor's next prompt, it was never answered, and
nothing can write it again -- the session it was meant for cannot be reopened, and a backend's
log of the reviewer is a record to read rather than a prompt to resume from. Only ever the one:
each round overwrites it, so what is kept is the review the actor has not been given and never
a history of the ones it has -- what became of those is in the repository, which is where the
next reviewer reads them off anyway.

A run the reviewer agreed with keeps nothing at all -- it clears what it held -- and cleared is
not the same as never written: humanize picks up from the last run that left an entry, empty or
not, so the next run here opens on its own task rather than falling back to a review from some
run before this one. Which is what over means: the work is done, and a fresh actor handed that
last review would be reading a report of work it cannot see and was not asked for.

The flow brings one skill, `skills/review-notes`, which is mounted onto every session either
agent opens: how to read a round of work against the repository it landed in, and how to write
the review the actor is then handed. It is the flow's rather than the machine's, so a fork of
this flow that wants its reviews written differently edits that file and runs.
"""

import time
from typing import Any, NamedTuple

from hmz.flows import Agent, flow
from pydantic import BaseModel, Field


class Agents(NamedTuple):
    """The two the flow drives: one that works in a session, and one that arrives fresh."""

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

#: How a picked-up run opens, once and only for its first round: the task, since the actor has
#: never been told it, and under it the review the last run stopped on, set apart as somebody
#: else's reading of work already there. Run together, the two would read as one instruction,
#: and the review is not one -- it is about a round this actor did not take, in a repository it
#: has not looked at yet. Nor is it said to be about this task: the state is the workspace's,
#: and neither this flow nor humanize knows what the run that left it was started on.
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
    # The actor remembers, and a session held across the rounds is how. A run picked up opens
    # a new one all the same -- a session is not something a flow can reopen -- so what the
    # actor of such a run knows is the prompt below and whatever the repository shows it.
    working = agents.actor.new()
    # The review the last run stopped on, where there was one, which nobody has answered.
    notes = state.get("notes") or ""
    # The task either way: a run that opened on the review alone would be one whose actor was
    # never told what it is for, since a review carries the task no further than it cites it.
    prompt = PICKED_UP.format(task=task, notes=notes) if notes else task
    while True:
        worked = working(prompt, suppress=True)
        # Only a landed turn earns a review, and the reviewer's is the actor's next prompt.
        # A turn that failed answers with nothing, so the round is taken again rather than
        # advanced past a review the actor never saw -- the opening one included.
        if worked:
            # A new session each round, so the reviewer reads the repository rather than its
            # own earlier reviews -- and is handed the task again, never having seen it.
            review = agents.reviewer(REVIEW_PROMPT + task, suppress=True, schema=Review)
            if review is not None and review.done:
                # The reviewer is the one that says it is over: the actor believing it has
                # finished is what earns a review, and this is the review agreeing.
                print(review.notes)
                # And what is over is not picked up: this review is a report of finished
                # work, and an actor handed it as a prompt is one with nothing to do. Emptied
                # rather than left, which is what the next run here is handed and reads as a
                # run to start clean rather than as a run to carry on.
                state.clear()
                return
            # A round nobody reviewed is a round taken again: the actor keeps the prompt it
            # had rather than being sent on by a review that was never written.
            if review is not None and review.notes:
                # Word for word, and the whole of the prompt: this session heard the task in
                # its first round, whichever run that round belonged to.
                prompt = notes = review.notes
            # Kept each round rather than at the end, because a run worth picking up is one
            # that was stopped: the review it stopped on is what the next run carries in.
            state.update(rounds=state.get("rounds", 0) + 1, notes=notes)
        time.sleep(5)
