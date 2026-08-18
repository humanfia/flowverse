"""Flame chase: two agents turn about, and the turn that is owed when a run is stopped.

Nothing here runs a coding agent. What is checked is the alternating -- a turn each, a session
apiece, neither of them reading the other's history -- and that a run picked up carries on with
the agent whose turn it was rather than starting the pair over at the first of them. The two
counters are read together: the turns the runs took are what the rounds they left behind are
counted against, so a round the machine went down in the middle of is one round to both of them.
"""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING, Any

import pytest
from hmz.agents import AgentBase, AgentConfig, Event, SessionBase
from hmz.flows import resumes

import flame_chase

if TYPE_CHECKING:
    import os
    from collections.abc import Iterator

    from pydantic import BaseModel

#: Every test here drives a `while True` loop and is ended by something the test itself
#: raises. A stop that never lands is a suite that hangs rather than one that fails, so the
#: clock is on all of them; a test that needs longer says so itself.
pytestmark = pytest.mark.timeout(60)


CONFIG = AgentConfig(model="m", effort="high")


class _Enough(Exception):  # noqa: N818  -- the way out of a loop that has no other
    """Raised out of the wait, or out of a turn, which is a run stopped either way."""


class _Scripted(AgentBase):
    """An agent that writes down that it was the one asked, and may be stopped mid-turn."""

    def __init__(self, name: str, taken: list[str], cut: int = 0) -> None:
        super().__init__(CONFIG, name=name)
        #: Whose turns went in what order, shared with the other agent: which of the two took
        #: a turn is the whole of what this suite reads a round off.
        self.taken = taken
        #: Which of its own turns is cut off partway, or 0 for an agent that finishes them --
        #: a machine going down under a turn, from inside which it is a turn that never ended.
        self.cut = cut
        self.turns = 0

    def new(self, cwd: str | os.PathLike[str] | None = None) -> _ScriptedSession:
        return _ScriptedSession(self, cwd)


#: What the stand-in names each session it opens, so that a test can count them: this flow
#: opens one per turn, neither agent carrying anything of its own into the next.
_NAMES = itertools.count(1)


class _ScriptedSession(SessionBase):
    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        del prompt, schema
        agent = self._agent
        assert isinstance(agent, _Scripted)
        agent.turns += 1
        agent.taken.append(agent.id)
        if agent.turns == agent.cut:
            raise _Enough
        self._adopt(f"session-{next(_NAMES)}")
        yield Event(kind="result", text="done what I could")


def _stopped_after(monkeypatch: pytest.MonkeyPatch, turns: int) -> None:
    """Ends the run in the wait after a turn, the way a machine going down ends one.

    Args:
      monkeypatch: What the wait is replaced through.
      turns: How many turns to let happen first, which is half as many rounds.
    """
    taken = itertools.count(1)

    def slept(seconds: float) -> None:
        if next(taken) >= turns:
            raise _Enough

    monkeypatch.setattr(flame_chase.time, "sleep", slept)


def _pair(taken: list[str], cut: int = 0) -> tuple[_Scripted, _Scripted]:
    """The two agents the flow drives, writing their turns into one list."""
    return _Scripted("one", taken, cut), _Scripted("two", taken)


def _run(
    agents: tuple[_Scripted, _Scripted], state: dict[str, Any], task: str = "add undo"
) -> None:
    """Runs the loop until the wait, or a turn that was cut off, ends it."""
    with pytest.raises(_Enough):
        flame_chase.run(agents, task, state)


def test_the_two_take_turns_each_in_a_session_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken: list[str] = []
    agents = _pair(taken)
    _stopped_after(monkeypatch, 4)

    _run(agents, {})

    assert taken == ["one", "two", "one", "two"]
    # A session per turn: an agent arriving reads the repository, not its own last turn.
    assert [len(agent.opened) for agent in agents] == [2, 2]


def test_what_it_leaves_behind_is_whose_turn_is_next_and_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round is a turn each, so three turns is one round and a second one half taken."""
    state: dict[str, Any] = {}
    _stopped_after(monkeypatch, 3)

    _run(_pair([]), state)

    assert state == {"rounds": 1, "turn": 1}
    assert json.loads(json.dumps(state)) == state  # what a cycle can write down


def test_a_run_picked_up_hands_the_turn_to_the_agent_that_is_owed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Or the first agent would take two turns in a row, which is the one thing to avoid."""
    state: dict[str, Any] = {}
    _stopped_after(monkeypatch, 1)
    first: list[str] = []
    _run(_pair(first), state)

    again: list[str] = []
    _stopped_after(monkeypatch, 2)
    _run(_pair(again), state)

    assert first == ["one"]
    assert again == ["two", "one"]
    assert state == {"rounds": 1, "turn": 1}  # one round, half taken by each run


def test_a_turn_cut_off_partway_is_taken_again_by_the_agent_whose_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn is handed on once it is over: a turn that never ended is still owed."""
    state: dict[str, Any] = {}
    _stopped_after(monkeypatch, 10)  # far enough off that the cut turn ends the run
    first: list[str] = []
    _run(_pair(first, cut=2), state)
    left = dict(state)

    again: list[str] = []
    _stopped_after(monkeypatch, 1)
    _run(_pair(again), state)

    assert first == ["one", "two", "one"]  # and the third of them never finished
    assert left == {"rounds": 1, "turn": 0}  # the cut turn's round is not counted
    assert again == ["one"]


def test_a_round_the_first_agent_was_cut_off_in_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two counters say one thing: four turns finished is two rounds, however they fell."""
    state: dict[str, Any] = {}
    _stopped_after(monkeypatch, 10)  # far enough off that the cut turn ends the run
    first: list[str] = []
    _run(_pair(first, cut=2), state)  # cut in the turn that opens the second round

    again: list[str] = []
    _stopped_after(monkeypatch, 2)
    _run(_pair(again), state)

    # The cut turn is not one of them, and the run picking up took it again: four turns
    # finished between the two runs, which is two rounds and cannot be read as three.
    assert [*first[:-1], *again] == ["one", "two", "one", "two"]
    assert state == {"rounds": 2, "turn": 0}


def test_it_says_it_can_be_picked_up_and_drives_two_agents() -> None:
    """Which is what hands it the dict at all: a flow that only took one would never see it."""
    from hmz.flows import drives

    assert resumes(flame_chase.__file__)
    assert drives(flame_chase.__file__) == ("", "")
