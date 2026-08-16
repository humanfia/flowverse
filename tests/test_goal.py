"""The goal loop: the task run as the agent's own goal, again and again.

Nothing here runs a coding agent. What is checked is that each round is a goal of its own, on
the task itself, and that a run of it that was stopped hands the next one the round it reached
and nothing besides -- there is nothing else a goal starting from scratch could use.
"""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING, Any

import pytest
from hmz.agents import AgentBase, AgentConfig, Event, SessionBase
from hmz.runner import resumes

import goal

if TYPE_CHECKING:
    import os
    from collections.abc import Iterator

    from pydantic import BaseModel

#: Every test here drives a `while True` loop and is ended by something the test itself
#: raises. A stop that never lands is a suite that hangs rather than one that fails, so the
#: clock is on all of them; a test that needs longer says so itself.
pytestmark = pytest.mark.timeout(60)


CONFIG = AgentConfig(model="m", effort="high")


class _Scripted(AgentBase):
    """An agent with a goal feature that does nothing but say it was given one."""

    #: What the flow declares its agent runs under, which a stand-in has to answer to.
    pursues = True

    def __init__(self) -> None:
        super().__init__(CONFIG, name="worker")
        #: Every objective it was set, in order, which is what a test reads the run off.
        self.pursued: list[str] = []

    def new(self, cwd: str | os.PathLike[str] | None = None) -> _ScriptedSession:
        return _ScriptedSession(self, cwd)


#: What the stand-in names each session it opens, so that a test can count them: a goal runs
#: in one of its own, and the loop is what starts the next.
_NAMES = itertools.count(1)


class _ScriptedSession(SessionBase):
    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        del prompt, schema
        yield Event(kind="result", text="")

    def _pursue(self, objective: str) -> str:
        agent = self._agent
        assert isinstance(agent, _Scripted)
        agent.pursued.append(objective)
        self._adopt(f"session-{next(_NAMES)}")
        return "as far as I got"


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

    monkeypatch.setattr(goal.time, "sleep", slept)


def _run(agent: _Scripted, state: dict[str, Any], task: str = "add undo") -> None:
    """Runs the loop until the wait says that is enough of it."""
    with pytest.raises(_Enough):
        goal.run(goal.Agents(worker=agent), task, state)


def test_every_round_is_the_task_set_as_a_goal_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _Scripted()
    _stopped_after(monkeypatch, 3)

    _run(agent, {})

    assert agent.pursued == ["add undo"] * 3
    # A session apiece: a goal that stopped short is started again rather than continued.
    assert len(agent.opened) == 3


def test_what_it_leaves_behind_is_the_round_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}
    _stopped_after(monkeypatch, 3)

    _run(_Scripted(), state)

    assert state == {"rounds": 3}
    assert json.loads(json.dumps(state)) == state  # what a cycle can write down


def test_a_run_picked_up_goes_on_from_the_round_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And starts its first goal on the task, having nothing of the last run to go on."""
    state: dict[str, Any] = {"rounds": 40}
    agent = _Scripted()
    _stopped_after(monkeypatch, 2)

    _run(agent, state)

    assert state == {"rounds": 42}
    assert agent.pursued == ["add undo"] * 2


def test_it_says_it_can_be_picked_up_and_runs_its_agent_under_a_goal() -> None:
    """Which is what hands it the dict at all: a flow that only took one would never see it."""
    from hmz.runner import drives

    assert resumes(goal.__file__)
    assert drives(goal.__file__) == ("worker",)
