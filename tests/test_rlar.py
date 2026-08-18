"""The RLAR loop: an actor that remembers, a reviewer that does not, and the way out.

Nothing here runs a coding agent. What is checked is the shape of the loop: the actor keeps one
session across the rounds, each round's review is the actor's next prompt word for word, the
run ends when the reviewer says the task is done rather than when the actor believes it is, and
a run that was stopped leaves behind the one review nobody has acted on -- which the run that
picks it up opens on, along with the task, since the actor picking it up is new.
"""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING

import pytest
from hmz.agents import AgentBase, AgentConfig, Event, SessionBase

import rlar

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterator

    from pydantic import BaseModel

#: Every test here drives a `while True` loop and is ended by something the test itself
#: raises. A stop that never lands is a suite that hangs rather than one that fails, so the
#: clock is on all of them; a test that needs longer says so itself.
pytestmark = pytest.mark.timeout(60)


CONFIG = AgentConfig(model="m", effort="high")


class _Scripted(AgentBase):
    """An agent whose every turn is what the test said it would be."""

    def __init__(self, name: str, doing: Callable[[str], str]) -> None:
        super().__init__(CONFIG, name=name)
        self.doing = doing
        #: Every prompt it was given, in order, which is what a test reads the run off.
        self.heard: list[str] = []

    def new(self, cwd: str | os.PathLike[str] | None = None) -> _ScriptedSession:
        return _ScriptedSession(self, cwd)


#: What the stand-in names each session it opens, so that a test can count them: a flow that
#: holds one session and one that opens one a round are what this suite is about.
_NAMES = itertools.count(1)


class _ScriptedSession(SessionBase):
    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        # A stand-in for a backend that cannot be held to a shape: it was asked for one in
        # the prompt, and what it says is read back.
        del schema
        agent = self._agent
        assert isinstance(agent, _Scripted)
        agent.heard.append(prompt)
        said = agent.doing(prompt)
        if said:  # a turn that landed opens the session, as it does on a real backend
            self._adopt(f"session-{next(_NAMES)}")
        yield Event(kind="result", text=said)


def _review(*, done: bool, notes: str) -> str:
    """One review, as the reviewer is held to answer it."""
    return json.dumps({"done": done, "notes": notes})


def _slept(seconds: float) -> None:
    """Stands in for the wait between rounds, which is for a real agent."""


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The five seconds between rounds are for a real agent, not for a suite."""
    monkeypatch.setattr(rlar.time, "sleep", _slept)


class _Enough(Exception):  # noqa: N818  -- the way out of a loop that has no other
    """Raised out of the wait, which is a run stopped rather than a run that ended."""


def _stopped_after(monkeypatch: pytest.MonkeyPatch, rounds: int) -> None:
    """Ends the run in the wait between rounds, the way a machine going down ends one.

    Args:
      monkeypatch: What the wait is replaced through.
      rounds: How many rounds to let happen first.
    """
    taken = itertools.count(1)

    def slept(seconds: float) -> None:
        if next(taken) >= rounds:
            raise _Enough

    monkeypatch.setattr(rlar.time, "sleep", slept)


def test_the_review_is_the_actors_next_prompt_and_the_reviewer_ends_the_run() -> None:
    rounds = {"at": 0}

    def reviewing(_: str) -> str:
        rounds["at"] += 1
        if rounds["at"] < 2:
            return _review(
                done=False, notes="undo works, redo does not: see editor.py:40"
            )
        return _review(done=True, notes="both work, and the tests cover them")

    actor = _Scripted("actor", lambda _: "done what I could")
    reviewer = _Scripted("reviewer", reviewing)

    rlar.run(rlar.Agents(actor=actor, reviewer=reviewer), "add undo/redo", {})

    # The task opens the run, and what the reviewer said is the next prompt, word for word.
    assert actor.heard == [
        "add undo/redo",
        "undo works, redo does not: see editor.py:40",
    ]
    # One session, held across the rounds: the actor is the side that has to remember.
    assert len(actor.opened) == 1
    # And a new one per round for the reviewer, which is the side that must not.
    assert len(reviewer.opened) == 2
    # Handed the task each time, never having seen it.
    assert all(prompt.startswith(rlar.REVIEW_PROMPT) for prompt in reviewer.heard)
    assert all("add undo/redo" in prompt for prompt in reviewer.heard)


def test_a_review_that_never_arrived_leaves_the_round_to_be_taken_again() -> None:
    """A turn that failed, and an answer that is not a review, are the same thing here."""
    rounds = {"at": 0}

    def reviewing(_: str) -> str:
        rounds["at"] += 1
        if rounds["at"] < 2:
            return "I could not tell, sorry."  # not a review, whatever else it is
        return _review(done=True, notes="it holds")

    actor = _Scripted("actor", lambda _: "done what I could")

    rlar.run(
        rlar.Agents(actor=actor, reviewer=_Scripted("reviewer", reviewing)),
        "add undo/redo",
        {},
    )

    # The actor is asked the same thing again rather than sent on by a review nobody wrote.
    assert actor.heard == ["add undo/redo", "add undo/redo"]


def test_a_turn_that_failed_is_not_reviewed() -> None:
    """Only a landed turn earns a review: there is nothing yet for a reviewer to read."""
    turns = {"at": 0}

    def working(_: str) -> str:
        turns["at"] += 1
        return "" if turns["at"] == 1 else "there you go"

    reviewer = _Scripted("reviewer", lambda _: _review(done=True, notes="it holds"))

    rlar.run(
        rlar.Agents(actor=_Scripted("actor", working), reviewer=reviewer),
        "add undo",
        {},
    )

    assert len(reviewer.heard) == 1  # the first round wrote nothing to review


def test_a_run_that_was_stopped_leaves_the_round_and_the_review_the_actor_is_owed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Written as the rounds go: a run worth picking up is one nobody let finish."""
    rounds = {"at": 0}

    def reviewing(_: str) -> str:
        rounds["at"] += 1
        return _review(
            done=False, notes=f"round {rounds['at']}: redo, see editor.py:40"
        )

    held: dict[str, object] = {}
    _stopped_after(monkeypatch, rounds=2)

    with pytest.raises(_Enough):
        rlar.run(
            rlar.Agents(
                actor=_Scripted("actor", lambda _: "done what I could"),
                reviewer=_Scripted("reviewer", reviewing),
            ),
            "add undo/redo",
            held,
        )

    # Which round it is on, and the one review nobody has acted on -- not round 1's, which
    # the actor was given and answered, and nothing of what either of them said besides,
    # which is the backends' own record to keep.
    assert held == {"rounds": 2, "notes": "round 2: redo, see editor.py:40"}


def test_a_run_picked_up_opens_on_the_task_and_the_review_it_carries() -> None:
    """Both, because the actor is new: the state is the workspace's, the task this run's.

    A workspace holds one state per flow and nothing about the task it was left under, so the
    review carried in may be of work nobody asked this run for. An actor told only that is an
    actor working on the last run's task.
    """
    rounds = {"at": 0}

    def reviewing(_: str) -> str:
        rounds["at"] += 1
        if rounds["at"] < 2:
            return _review(
                done=False, notes="the CLI page is still empty: see docs/cli.md"
            )
        return _review(done=True, notes="it holds")

    actor = _Scripted("actor", lambda _: "done what I could")
    reviewer = _Scripted("reviewer", reviewing)

    rlar.run(
        rlar.Agents(actor=actor, reviewer=reviewer),
        "write the docs",
        {"rounds": 3, "notes": "redo is still not undone: see editor.py:40"},
    )

    opening = actor.heard[0]
    # The task this run was started on, which nothing else here would tell the actor.
    assert opening.startswith("write the docs")
    # And the review it is carrying, word for word and last, said to be a review of an
    # earlier round rather than run on to the task as if it were more of it.
    assert opening.endswith("redo is still not undone: see editor.py:40")
    assert "review" in opening.removeprefix("write the docs").lower()
    # One session, which has now heard the task: every round after the first is the review
    # word for word, exactly as in a run that was picked up from nothing.
    assert actor.heard[1:] == ["the CLI page is still empty: see docs/cli.md"]
    assert len(actor.opened) == 1
    # The task is the reviewer's to hold, and it is handed it as it always is.
    assert all("write the docs" in prompt for prompt in reviewer.heard)


def test_what_a_run_the_reviewer_agreed_with_clears_is_not_picked_up_again() -> None:
    """It is over, and the run after it opens on its own task rather than on this one's.

    Cleared is not the same as never written: humanize picks a flow up from the last run
    that left an entry, empty or not, so an emptied state is what the next run is handed --
    which is this run saying the next one starts clean, and being answered.
    """
    held: dict[str, object] = {"rounds": 3, "notes": "redo is still not undone"}
    agreed = _Scripted(
        "reviewer", lambda _: _review(done=True, notes="tests cover them")
    )

    rlar.run(
        rlar.Agents(
            actor=_Scripted("actor", lambda _: "done what I could"), reviewer=agreed
        ),
        "add undo/redo",
        held,
    )

    assert held == {}

    # The next run in that workspace, on whatever it was started on: an actor handed the
    # review above would be reading a report of work that is finished and not its own.
    actor = _Scripted("actor", lambda _: "done what I could")
    rlar.run(rlar.Agents(actor=actor, reviewer=agreed), "write the docs", held)

    assert actor.heard == ["write the docs"]


def test_the_mark_and_the_signature_both_say_it_can_be_picked_up() -> None:
    """The mark and the signature go together: one without the other is a run that raises."""
    import inspect

    from hmz.flows import drives, resumes

    assert resumes(rlar.__file__)
    assert drives(rlar.__file__) == ("actor", "reviewer")
    # Last, which is where a flow that says it can be picked up is handed the dict.
    assert list(inspect.signature(rlar.run).parameters)[-1] == "state"
