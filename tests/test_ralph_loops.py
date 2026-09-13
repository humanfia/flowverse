from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import ralph_loop
import stateful_ralph
from hmz.flows import (
    Allowance,
    Stopped,
    Usage,
    configures,
    declared,
    drives,
    held,
    offered,
    resumes,
)

FLOWS = Path(__file__).parents[1] / "flows"

#: What one turn that landed is said to have come out with, which is a millionth of an
#: allowance written in millions -- so an allowance of 3 is three rounds of this agent.
EACH = 1_000_000.0


class FakeSession:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *, suppress: bool = False) -> Any:
        # Before the turn, as `SessionBase` reads it: a turn taken once the run's allowance
        # is spent raises rather than answering, and `Stopped` is not a failed turn, so
        # `suppress` does not swallow it. Which is the whole of what ends these loops now.
        self.agent.allowed()
        self.prompts.append(prompt)
        return self.agent.answer()


class FakeAgent:
    """A turn that costs a million output tokens, or answers with nothing and costs nothing.

    A turn that could not be taken is what `answers` says: under `suppress` it comes back
    empty and spends nothing, which is the case an allowance in tokens cannot end a loop on.

    It stands in for the seam as well as for the backend: `allowance` is what the run was
    given, and a turn taken once it is spent raises exactly where a real session would.
    """

    def __init__(
        self, answers: list[Any] | None = None, allowance: Allowance | None = None
    ) -> None:
        self.answers = answers
        self.allowance = allowance or Allowance()
        self.sessions: list[FakeSession] = []
        self.turns = 0
        self.landed = 0

    def allowed(self) -> None:
        if self.allowance.over(output=self.spent().output):
            raise Stopped(self.allowance.over(output=self.spent().output))

    def answer(self) -> Any:
        if self.answers is None:
            said: Any = "worked"
        else:
            said = self.answers[self.turns] if self.turns < len(self.answers) else None
        self.turns += 1
        self.landed += bool(said)
        return said

    def new(self, cwd: str | Path | None = None) -> FakeSession:
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    def __call__(self, prompt: str, *, suppress: bool = False) -> Any:
        return self.new()(prompt, suppress=suppress)

    def spent(self) -> Usage:
        return Usage(output=EACH * self.landed)


@pytest.fixture(autouse=True)
def _instant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ralph_loop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(stateful_ralph.time, "sleep", lambda _seconds: None)


def said(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(("round ", "stopping: "))
    ]


@pytest.mark.parametrize("name", ["ralph_loop", "stateful_ralph"])
def test_a_loop_is_one_agent_resumable_and_holds_itself_to_nothing(name: str) -> None:
    base = FLOWS / name / "__init__.py"

    assert drives(base) == ("",)
    assert resumes(base)
    # Nothing to set up any more: what a run of it may spend is the run's setting rather
    # than the flow's, and the budget was the only thing this flow ever took.
    assert configures(base) is None
    # What it declares is the default it has always come with, which whoever runs it
    # overrides -- and which the flow itself never holds itself to.
    assert declared(base) == Allowance(tokens=10.0)
    assert [flow.name for flow in held(base)] == [""]
    assert name in offered(FLOWS)


def test_a_ralph_loop_opens_a_session_a_round_and_stops_on_the_runs_allowance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = FakeAgent(allowance=Allowance(tokens=3))
    kept: dict[str, Any] = {}

    # Raised out of the flow rather than caught by it: a run stopped for having spent what
    # it was given is a run that stopped, and `Runner` files it as one.
    with pytest.raises(Stopped):
        ralph_loop.run((agent,), "do the thing", kept)

    assert len(agent.sessions) == 4  # nothing carries over, so a session a round
    assert said(capsys) == ["round 1", "round 2", "round 3", "round 4"]
    # Left rather than emptied: a run stopped by its allowance is one to pick up, under a
    # fresh allowance, rather than one that is over.
    assert kept == {"rounds": 4}


def test_a_loop_picked_up_carries_the_round_it_reached(
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept: dict[str, Any] = {"rounds": 40}

    with pytest.raises(Stopped):
        ralph_loop.run((FakeAgent(allowance=Allowance(tokens=1)),), "do the thing", kept)

    # It is round 41 rather than round 1, and the allowance is this run's own rather than
    # what every run of it here has spent between them.
    assert said(capsys) == ["round 41", "round 42"]
    assert kept == {"rounds": 42}


def test_a_loop_whose_rounds_all_answer_with_nothing_gives_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept: dict[str, Any] = {}

    # A token allowance cannot end this one: a round that failed spends nothing to count.
    ralph_loop.run((FakeAgent([]),), "do the thing", kept)

    assert said(capsys) == [
        "round 1",
        "round 2",
        "round 3",
        "stopping: 3 rounds in a row answered with nothing",
    ]
    # Stopped rather than over: what stopped it is a thing to fix and carry on from.
    assert kept == {"rounds": ralph_loop.STALLED}


def test_a_round_that_answered_puts_the_run_of_empty_ones_back(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Three in a row rather than three in all, so it is rounds four to six that end it.
    agent = FakeAgent([None, None, "worked"])

    ralph_loop.run((agent,), "do the thing", {})

    assert said(capsys) == [
        *(f"round {each}" for each in range(1, 7)),
        "stopping: 3 rounds in a row answered with nothing",
    ]


def test_stateful_ralph_holds_one_session_for_every_round(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = FakeAgent(allowance=Allowance(tokens=2))
    kept: dict[str, Any] = {}

    with pytest.raises(Stopped):
        stateful_ralph.run((agent,), "do the thing", kept)

    (session,) = agent.sessions  # one conversation, every round of it
    assert session.prompts == ["do the thing", "do the thing"]
    assert said(capsys) == ["round 1", "round 2", "round 3"]
    assert kept == {"rounds": 3}


def test_stateful_ralph_gives_up_on_a_run_of_nothing_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept: dict[str, Any] = {}

    stateful_ralph.run((FakeAgent([]),), "do the thing", kept)

    assert said(capsys) == [
        "round 1",
        "round 2",
        "round 3",
        "stopping: 3 rounds in a row answered with nothing",
    ]
    assert kept == {"rounds": stateful_ralph.STALLED}


@pytest.mark.parametrize("said_", [{"hours": -1}, {"tokens": -1}, {"dollars": -1}])
def test_an_allowance_below_nothing_is_refused(said_: dict[str, float]) -> None:
    """Where it is written, rather than as a run that stops before it has started."""
    with pytest.raises(ValueError, match="less than nothing"):
        Allowance(**said_)
