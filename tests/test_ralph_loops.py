from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import ralph_loop
import stateful_ralph
from hmz.flows import Usage, configures, drives, held, offered, resumes

FLOWS = Path(__file__).parents[1] / "flows"

#: What one turn that landed is said to have come out with, which is a millionth of a budget
#: written in millions -- so a budget of 3 is three rounds of this agent.
EACH = 1_000_000.0


class FakeSession:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *, suppress: bool = False) -> Any:
        self.prompts.append(prompt)
        return self.agent.answer()


class FakeAgent:
    """A turn that costs a million output tokens, or answers with nothing and costs nothing.

    A turn that could not be taken is what `answers` says: under `suppress` it comes back
    empty and spends nothing, which is the case the budget cannot end a loop on.
    """

    def __init__(self, answers: list[Any] | None = None) -> None:
        self.answers = answers
        self.sessions: list[FakeSession] = []
        self.turns = 0
        self.landed = 0

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
def test_a_loop_is_one_agent_resumable_and_budgeted(name: str) -> None:
    base = FLOWS / name / "__init__.py"

    assert drives(base) == ("",)
    assert resumes(base)
    config = configures(base)
    assert config is not None
    assert set(config.model_fields) == {"budget"}
    assert config().budget == 10.0
    assert [flow.name for flow in held(base)] == [""]
    assert name in offered(FLOWS)


def test_a_ralph_loop_opens_a_session_a_round_and_stops_on_its_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = FakeAgent()
    kept: dict[str, Any] = {}

    ralph_loop.run((agent,), "do the thing", ralph_loop.Config(budget=3), kept)

    assert len(agent.sessions) == 3  # nothing carries over, so a session a round
    assert said(capsys) == [
        "round 1",
        "round 2",
        "round 3",
        "stopping: 3.00M output tokens of 3M",
    ]
    # Emptied rather than left: a loop that spent what it was given is over, and the next
    # run here opens at round one on a budget of its own.
    assert kept == {}


def test_a_loop_picked_up_carries_the_round_it_reached_and_what_it_spent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept: dict[str, Any] = {"rounds": 40, "output": 2 * EACH}

    ralph_loop.run((FakeAgent(),), "do the thing", ralph_loop.Config(budget=3), kept)

    # One round is what was left of the budget, and it is round 41 rather than round 1.
    assert said(capsys) == ["round 41", "stopping: 3.00M output tokens of 3M"]
    assert kept == {}


def test_a_loop_whose_rounds_all_answer_with_nothing_gives_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept: dict[str, Any] = {}

    # The budget cannot end this one: a round that failed spends nothing to be counted.
    ralph_loop.run((FakeAgent([]),), "do the thing", ralph_loop.Config(budget=0), kept)

    assert said(capsys) == [
        "round 1",
        "round 2",
        "round 3",
        "stopping: 3 rounds in a row answered with nothing",
    ]
    # Stopped rather than over: what stopped it is a thing to fix and carry on from.
    assert kept == {"rounds": ralph_loop.STALLED, "output": 0.0}


def test_a_round_that_answered_puts_the_run_of_empty_ones_back(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Three in a row rather than three in all, so it is rounds four to six that end it.
    agent = FakeAgent([None, None, "worked"])

    ralph_loop.run((agent,), "do the thing", ralph_loop.Config(budget=0), {})

    assert said(capsys) == [
        *(f"round {each}" for each in range(1, 7)),
        "stopping: 3 rounds in a row answered with nothing",
    ]


def test_stateful_ralph_holds_one_session_for_every_round(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = FakeAgent()
    kept: dict[str, Any] = {}

    stateful_ralph.run((agent,), "do the thing", stateful_ralph.Config(budget=2), kept)

    (session,) = agent.sessions  # one conversation, both rounds of it
    assert session.prompts == ["do the thing", "do the thing"]
    assert said(capsys) == [
        "round 1",
        "round 2",
        "stopping: 2.00M output tokens of 2M",
    ]
    assert kept == {}


def test_stateful_ralph_gives_up_on_a_run_of_nothing_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept: dict[str, Any] = {}

    stateful_ralph.run(
        (FakeAgent([]),), "do the thing", stateful_ralph.Config(budget=0), kept
    )

    assert said(capsys) == [
        "round 1",
        "round 2",
        "round 3",
        "stopping: 3 rounds in a row answered with nothing",
    ]
    assert kept == {"rounds": stateful_ralph.STALLED, "output": 0.0}


@pytest.mark.parametrize("module", [ralph_loop, stateful_ralph])
def test_a_budget_below_nothing_is_refused(module: Any) -> None:
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        module.Config(budget=-1)
