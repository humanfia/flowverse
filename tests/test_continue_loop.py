"""The continue loop: the task until a turn lands, "continue" after it, and what a run leaves.

Nothing here runs a coding agent. What is checked is the shape of the loop -- one session, held
across the rounds and nudged rather than told the task twice -- and what a run of it that was
stopped hands the next one: the round it reached, and nothing else. A run picked up opens a
session of its own, so it opens on the task exactly as a first run does.
"""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING, Any

import pytest
from hmz.agents import AgentBase, AgentConfig, Event, SessionBase
from hmz.runner import resumes

import continue_loop

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

    def __init__(self, doing: Callable[[str], str]) -> None:
        super().__init__(CONFIG, name="worker")
        self.doing = doing
        #: Every prompt it was given, in order, which is what a test reads the run off.
        self.heard: list[str] = []

    def new(self, cwd: str | os.PathLike[str] | None = None) -> _ScriptedSession:
        return _ScriptedSession(self, cwd)


#: What the stand-in names each session it opens, so that a test can count them: this flow
#: holds one for the length of a run, and a run picked up is a run with one of its own.
_NAMES = itertools.count(1)


class _ScriptedSession(SessionBase):
    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        del schema
        agent = self._agent
        assert isinstance(agent, _Scripted)
        agent.heard.append(prompt)
        said = agent.doing(prompt)
        if said:  # a turn that landed opens the session, as it does on a real backend
            self._adopt(f"session-{next(_NAMES)}")
        yield Event(kind="result", text=said)


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

    monkeypatch.setattr(continue_loop.time, "sleep", slept)


def _run(agent: _Scripted, state: dict[str, Any], task: str = "add undo") -> None:
    """Runs the loop until the wait says that is enough of it."""
    with pytest.raises(_Enough):
        continue_loop.run((agent,), task, state)


def test_the_task_is_sent_until_a_turn_lands_and_then_it_is_a_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = itertools.count(1)
    agent = _Scripted(lambda _: "" if next(turns) == 1 else "done what I could")
    _stopped_after(monkeypatch, 3)

    _run(agent, {})

    # The failed turn opened a session that never saw the task, so it is sent again.
    assert agent.heard == ["add undo", "add undo", "continue"]
    assert len(agent.opened) == 1  # one session, held across the rounds


def test_what_it_leaves_behind_is_the_round_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}
    _stopped_after(monkeypatch, 3)

    _run(_Scripted(lambda _: "done what I could"), state)

    assert state == {"rounds": 3}
    assert json.loads(json.dumps(state)) == state  # what a cycle can write down


def test_a_run_picked_up_goes_on_from_the_round_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {"rounds": 40}
    _stopped_after(monkeypatch, 2)

    _run(_Scripted(lambda _: "done what I could"), state)

    assert state == {"rounds": 42}


def test_a_run_picked_up_opens_on_the_task_rather_than_a_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session is opened rather than reopened, and a new one has heard nothing."""
    state: dict[str, Any] = {}
    _stopped_after(monkeypatch, 2)
    _run(_Scripted(lambda _: "done what I could"), state)

    picked_up = _Scripted(lambda _: "done what I could")
    _stopped_after(monkeypatch, 2)
    _run(picked_up, state)

    assert picked_up.heard == ["add undo", "continue"]
    # Which is what the state must not hold: a run that knew the task had been sent would
    # have opened on "continue", into a session with nothing to continue.
    assert set(state) == {"rounds"}


def test_it_says_it_can_be_picked_up_and_drives_the_one_agent() -> None:
    """Which is what hands it the dict at all: a flow that only took one would never see it."""
    from hmz.runner import drives

    assert resumes(continue_loop.__file__)
    assert drives(continue_loop.__file__) == ("",)
