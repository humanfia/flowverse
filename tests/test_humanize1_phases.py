"""The two phases in front of the loop, each run as a flow of its own.

`gen-idea` and `gen-plan` used to be halves of one run that also built the plan, and what
passed between them was a variable. They are two flows now, and what passes between them is
the file -- so what is checked here is that each one starts from a file that is there, writes
the file the next one starts from, and refuses before the first turn where it cannot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pytest
from hmz.agents import EVERYWHERE, AgentBase, AgentConfig, Event, SessionBase

import humanize1

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from pydantic import BaseModel

CONFIG = AgentConfig(model="m", effort="high")

#: What the analyst answers the draft relevance check with, which is asked for as a shape.
_RELEVANT = '{"relevant": true, "why": "it is about this repository"}'

#: And what it answers a convergence round with, settled the first time round.
_CONVERGED = '{"converged": true, "review": "AGREE\\n\\nnothing left to argue over"}'


class Scripted(AgentBase):
    """An agent whose every turn is what the test said it would be."""

    moments: ClassVar = EVERYWHERE

    def __init__(
        self, *, name: str, doing: Callable[[str], str] = lambda _: "done"
    ) -> None:
        super().__init__(CONFIG, name=name)
        self.doing = doing
        #: Every prompt it was given, in order, which is what a test reads the run off.
        self.heard: list[str] = []

    def new(self, cwd: str | os.PathLike[str] | None = None) -> ScriptedSession:
        return ScriptedSession(self, cwd)


class ScriptedSession(SessionBase):
    """One conversation with a scripted agent."""

    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        # Nothing is done with the shape: what is asked for is asked for in the prompt,
        # and what comes back is read as that shape by whatever asked.
        del schema
        agent = self._agent
        assert isinstance(agent, Scripted)
        agent.heard.append(prompt)
        yield Event(kind="result", text=agent.doing(prompt))


def _analyst(said: list[str]) -> Callable[[str], str]:
    """An analyst that finds the draft relevant and the first plan already converged."""

    def turn(prompt: str) -> str:
        if "relevant" in prompt.lower() and "REQUIRED_CHANGES" not in prompt:
            said.append("relevance")
            return _RELEVANT
        if "REQUIRED_CHANGES" in prompt:
            said.append("convergence")
            return _CONVERGED
        said.append("analysis")
        return "what the repository does today, and where undo would go"

    return turn


# ------------------------------------------------------------------------------------
# gen-idea
# ------------------------------------------------------------------------------------


def test_the_drafter_is_told_where_the_draft_goes_and_how_wide_to_open_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--n` and `--output` are the whole of what the command takes, and both reach the turn."""
    monkeypatch.chdir(tmp_path)
    drafter = Scripted(name="drafter")

    humanize1.gen_idea(
        humanize1.Drafting(drafter), "add undo", humanize1.Idea(n=4, output="draft.md")
    )

    (told,) = drafter.heard
    assert "4" in told
    assert str(tmp_path / "draft.md") in told


def test_a_draft_nobody_named_lands_where_the_plugin_puts_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `.humanize/ideas`, named after the idea, which is what `gen-plan` looks in."""
    monkeypatch.chdir(tmp_path)
    drafter = Scripted(name="drafter")

    humanize1.gen_idea(humanize1.Drafting(drafter), "add undo to the editor")

    (told,) = drafter.heard
    assert f"{humanize1.IDEAS}/add-undo-to-the-editor-" in told.replace(
        f"{tmp_path}/", ""
    )


def test_an_idea_that_was_not_given_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is nothing to open, and a turn spent finding that out is a turn wasted."""
    monkeypatch.chdir(tmp_path)
    drafter = Scripted(name="drafter")

    with pytest.raises(ValueError, match="given none"):
        humanize1.gen_idea(humanize1.Drafting(drafter), "   ")

    assert drafter.heard == []


def test_a_draft_would_not_be_written_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's own IO check: an output that is already there is a path to choose again."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "draft.md").write_text("something somebody wrote")

    with pytest.raises(ValueError, match="already exists"):
        humanize1.gen_idea(
            humanize1.Drafting(Scripted(name="drafter")),
            "add undo",
            humanize1.Idea(output="draft.md"),
        )


# ------------------------------------------------------------------------------------
# gen-plan
# ------------------------------------------------------------------------------------


def _drafted(root: Path, said: str = "a draft of the undo work") -> Path:
    """Writes a draft where `gen-idea` would have left one."""
    ideas = root / humanize1.IDEAS
    ideas.mkdir(parents=True, exist_ok=True)
    where = ideas / "add-undo-20260101-000000.md"
    where.write_text(said)
    return where


def test_the_plan_is_written_from_the_draft_the_idea_phase_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which is the whole of what makes them two flows rather than one: the file between."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    draft = _drafted(tmp_path)
    asked: list[str] = []
    planner = Scripted(name="planner")

    humanize1.gen_plan(
        humanize1.Planning(planner, Scripted(name="analyst", doing=_analyst(asked))),
        "add undo",
    )

    plan = tmp_path / humanize1.PLAN
    assert plan.is_file()
    # The draft stays in the plan file, as the plugin copies it in: it is the human input.
    assert draft.read_text() in plan.read_text()
    # Relevance first, then the analysis, then one convergence round that settled it.
    assert asked == ["relevance", "analysis", "convergence"]
    assert planner.heard  # and the planner held one session for the whole of it


def test_the_draft_to_plan_from_may_be_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--input`, for a draft somebody read and edited before asking for a plan."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    _drafted(tmp_path, "the one that was not asked for")
    mine = tmp_path / "mine.md"
    mine.write_text("the one that was")

    humanize1.gen_plan(
        humanize1.Planning(
            Scripted(name="planner"), Scripted(name="analyst", doing=_analyst([]))
        ),
        "add undo",
        humanize1.Plan(input="mine.md"),
    )

    assert "the one that was" in (tmp_path / humanize1.PLAN).read_text()


def test_a_plan_asked_for_with_no_draft_anywhere_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the first turn: the phase in front of it has not been run."""
    monkeypatch.chdir(tmp_path)
    analyst = Scripted(name="analyst", doing=_analyst([]))

    with pytest.raises(ValueError, match="no draft to plan from"):
        humanize1.gen_plan(
            humanize1.Planning(Scripted(name="planner"), analyst), "add undo"
        )

    assert analyst.heard == []


def test_a_draft_about_something_else_entirely_stops_the_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin will not plan from a draft that is not about this repository."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    _drafted(tmp_path)

    def elsewhere(prompt: str) -> str:
        del prompt
        return '{"relevant": false, "why": "it is about a different project"}'

    with pytest.raises(ValueError, match="does not appear to be related"):
        humanize1.gen_plan(
            humanize1.Planning(
                Scripted(name="planner"), Scripted(name="analyst", doing=elsewhere)
            ),
            "add undo",
        )
