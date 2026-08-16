"""One whole RLCR loop, driven by agents that do what the test tells them to.

Nothing here runs a coding agent. The loop is a hook on the moment a turn stops, so a fake
agent that writes the files a round writes and then stops takes the loop through every phase
it has: a round is reviewed, the review says the work is done, the code review finds nothing,
and the finalize round ends the loop -- which is the path the plugin calls `complete`.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from hmz.agents import (
    EVERYWHERE,
    AgentBase,
    AgentConfig,
    Event,
    Moment,
    Occasion,
    SessionBase,
    Stopped,
    Verdict,
)

import humanize1
from _humanize1 import loop

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from pydantic import BaseModel

CONFIG = AgentConfig(model="m", effort="high")

#: A plan with a goal and a criterion, which is what the loop is anchored to.
PLAN = """# Add undo to the editor

## Goal Description
The editor can undo the last edit.

## Acceptance Criteria
- AC-1: `undo()` restores the previous buffer.
  - Positive Tests: undo after one edit restores the buffer
  - Negative Tests: undo with no edits does nothing

## Task Breakdown
| Task ID | Description | Target AC | Tag | Depends On |
|---------|-------------|-----------|-----|------------|
| task1 | write undo | AC-1 | coding | - |
"""

#: What a round has to leave behind for the gates to let it stop.
SUMMARY = """# Round {round} Summary

## What Was Implemented
undo, as the plan says

## Files Changed
- editor.py

## Validation
- the tests pass

## Remaining Items
- none

## BitLesson Delta
- Action: none
- Lesson ID(s): NONE
- Notes: nothing new was learned
"""

CONTRACT = """# Round {round} Contract

- Mainline Objective: implement undo
- Target ACs: AC-1
- Blocking Side Issues In Scope: none
- Queued Side Issues Out of Scope: none
- Success Criteria: undo restores the buffer
"""

#: A tracker with the placeholders filled in, which is what round 0 is asked to write.
TRACKER = """# Goal Tracker

## IMMUTABLE SECTION

### Ultimate Goal
The editor can undo the last edit.

### Acceptance Criteria
- AC-1: `undo()` restores the previous buffer.

---

## MUTABLE SECTION

#### Active Tasks
| Task | Target AC | Status | Tag | Owner | Notes |
|------|-----------|--------|-----|-------|-------|
| [mainline] write undo | AC-1 | in_progress | coding | claude | - |
"""


class Scripted(AgentBase):
    """An agent whose every turn is what the test said it would be."""

    #: Everything, including the moment a tool can actually be refused: the flow says the
    #: builder has to run it, and a stand-in that did not could not be the builder.
    moments: ClassVar = EVERYWHERE | {Moment.PERMISSION_REQUEST}

    def __init__(
        self, config: AgentConfig, *, name: str, doing: Callable[[str], str]
    ) -> None:
        """Initializes it.

        Args:
          config: What it says it runs.
          name: What the flow calls it.
          doing: What one turn of it does, given the prompt, answering with what it says.
        """
        super().__init__(config, name=name)
        self.doing = doing
        #: Every prompt it was given, in order, which is what a test reads the run off.
        self.heard: list[str] = []

    def new(self, cwd: str | os.PathLike[str] | None = None) -> ScriptedSession:
        """Opens a conversation."""
        return ScriptedSession(self, cwd)


class ScriptedSession(SessionBase):
    """One conversation with a scripted agent."""

    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        # Nothing is done with the shape: this stands in for a backend that cannot be held to
        # one, so the prompt it is given already asks for it and what it says is read back.
        del schema
        agent = self._agent
        assert isinstance(agent, Scripted)
        agent.heard.append(prompt)
        yield Event(kind="result", text=agent.doing(prompt))


def _git(*args: str, at: Path) -> None:
    """Runs one git command, failing the test if it fails."""
    subprocess.run(["git", "-C", str(at), *args], check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A git repository with a plan committed in it, which is what the loop needs to start.

    Committed, so the runs below say `track_plan_file` -- which is the plugin's own rule: a
    plan is either in git and declared to be, or gitignored, and a loop started against the
    other one is refused before it can build anything against a plan nothing is watching.
    """
    _git("init", "-b", "main", at=tmp_path)
    _git("config", "user.email", "t@example.com", at=tmp_path)
    _git("config", "user.name", "t", at=tmp_path)
    (tmp_path / ".gitignore").write_text(".humanize*\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plan.md").write_text(PLAN)
    (tmp_path / "editor.py").write_text("def undo():\n    pass\n")
    _git("add", "-A", at=tmp_path)
    _git("commit", "-m", "the plan", at=tmp_path)
    return tmp_path


def _builder(where: Path, wrote: list[str]) -> Callable[[str], str]:
    """A builder that writes what a round writes and commits it, then says it is done.

    Args:
      where: The workspace.
      wrote: Filled in with what each turn wrote, so a test can see which rounds happened.

    Returns:
      What one turn of it does.
    """
    turns = {"at": 0}

    def turn(prompt: str) -> str:
        turns["at"] += 1
        (found,) = sorted((where / ".humanize" / "rlcr").iterdir())
        if "Finalize Phase" in prompt:
            (found / "finalize-summary.md").write_text(
                "# Finalize\n\nsimplified nothing\n"
            )
            wrote.append("finalize")
            return "finalized"
        at = 0
        for line in prompt.splitlines():
            if "round-" in line and "-summary.md" in line:
                at = int(line.split("round-")[1].split("-summary")[0])
                break
        (found / f"round-{at}-summary.md").write_text(SUMMARY.format(round=at))
        (found / f"round-{at}-contract.md").write_text(CONTRACT.format(round=at))
        (found / "goal-tracker.md").write_text(TRACKER)
        # Something different every turn, so that every round has a commit of its own --
        # a round that changed nothing is one the clean check would let through anyway.
        (where / "editor.py").write_text(f"def undo():\n    return {turns['at']}\n")
        _git("add", "editor.py", at=where)
        _git("commit", "-m", f"round {at}", at=where)
        wrote.append(f"round-{at}")
        return f"round {at} done"

    return turn


#: What a reviewer answers the plan compliance check with, which the flow asks for as a shape
#: rather than as a verdict line: a scripted agent answers in it the way a real one is held to.
_COMPLIES = '{{"relevant": true, "switches_branch": false, "why": "{why}."}}'


def _reviewer(said: list[str]) -> Callable[[str], str]:
    """A reviewer that accepts the work and then finds nothing wrong with the code.

    Args:
      said: Filled in with what it was asked about, in order.

    Returns:
      What one turn of it does.
    """

    def turn(prompt: str) -> str:
        if prompt.startswith("You are a specialized agent that validates"):
            said.append("compliance")
            return _COMPLIES.format(
                why="the plan adds undo to the editor in this repository"
            )
        if prompt.startswith("# Code Review Phase"):
            said.append("code-review")
            return "Reviewed the whole change. Nothing to fix before this ships."
        said.append("summary-review")
        return (
            "Everything the summary claims holds.\n\n"
            "Mainline Progress Verdict: ADVANCED\n\nCOMPLETE"
        )

    return turn


@pytest.mark.timeout(120)
def test_a_loop_that_is_accepted_runs_to_complete(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round, review, code review, finalize: the whole of the plugin's happy path."""
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    asked: list[str] = []
    agents = humanize1.Building(
        builder=Scripted(CONFIG, name="builder", doing=_builder(workspace, wrote)),
        reviewer=Scripted(CONFIG, name="reviewer", doing=_reviewer(asked)),
        human=humanize1.HumanAgent(),
    )
    config = humanize1.Rlcr(
        plan_file="docs/plan.md",
        track_plan_file=True,
        skip_quiz=True,
        privacy=True,
    )

    humanize1.rlcr(agents, "add undo", config)

    (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
    # The loop ended where the plugin ends a loop that passed: the state file says so.
    assert (found / "complete-state.md").is_file()
    assert not (found / "state.md").exists()
    # Round 0, its review, the code review, and the finalize round it ends on.
    assert wrote == ["round-0", "finalize"]
    assert asked == ["compliance", "summary-review", "code-review"]
    # And it left the files the plugin leaves, under the names it leaves them under.
    assert (found / "round-0-prompt.md").is_file()
    assert (found / "round-0-review-prompt.md").is_file()
    assert (found / "round-0-review-result.md").is_file()
    assert (found / "goal-tracker.md").is_file()
    assert (found / "plan.md").read_text() == PLAN
    assert (
        found / ".review-phase-started"
    ).read_text().strip() == "build_finish_round=0"


@pytest.mark.timeout(120)
def test_a_round_that_wrote_nothing_is_sent_back_rather_than_reviewed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap gates run before the expensive review, which is why they are gates.

    The summary itself is not what is missing: the setup writes the scaffold for round 0, as
    the plugin's own setup script does, so what a round that did nothing is short of is the
    contract it was told to state before it started.
    """
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    asked: list[str] = []
    building = _builder(workspace, wrote)
    turns = {"at": 0}

    def forgetful(prompt: str) -> str:
        turns["at"] += 1
        if turns["at"] == 1:
            return "I think I am done"  # nothing written, nothing committed
        return building(prompt)

    agents = humanize1.Building(
        builder=Scripted(CONFIG, name="builder", doing=forgetful),
        reviewer=Scripted(CONFIG, name="reviewer", doing=_reviewer(asked)),
        human=humanize1.HumanAgent(),
    )

    humanize1.rlcr(
        agents,
        "add undo",
        humanize1.Rlcr(
            plan_file="docs/plan.md",
            track_plan_file=True,
            skip_quiz=True,
            privacy=True,
        ),
    )

    builder = agents.builder
    assert isinstance(builder, Scripted)
    # The second thing it heard is the refusal, in the plugin's own words, and no review was
    # run to produce it.
    assert "Round Contract Missing" in builder.heard[1]
    assert asked[1] == "summary-review"
    assert (
        min((workspace / ".humanize" / "rlcr").iterdir()) / "complete-state.md"
    ).is_file()


@pytest.mark.timeout(120)
def test_findings_send_the_builder_back_before_the_loop_can_finish(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `[P0-9]` in the code review is a round of its own, and the loop does not end on it."""
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    reviews = {"at": 0}

    def reviewing(prompt: str) -> str:
        if prompt.startswith("You are a specialized agent that validates"):
            return _COMPLIES.format(why="it is this repository's")
        if prompt.startswith("# Code Review Phase"):
            reviews["at"] += 1
            if reviews["at"] == 1:
                return (
                    "Looked at the diff.\n- [P1] undo returns a number - editor.py:2\n"
                )
            return "Fixed. Nothing left to raise."
        return "Holds.\n\nMainline Progress Verdict: ADVANCED\n\nCOMPLETE"

    agents = humanize1.Building(
        builder=Scripted(CONFIG, name="builder", doing=_builder(workspace, wrote)),
        reviewer=Scripted(CONFIG, name="reviewer", doing=reviewing),
        human=humanize1.HumanAgent(),
    )

    humanize1.rlcr(
        agents,
        "add undo",
        humanize1.Rlcr(
            plan_file="docs/plan.md",
            track_plan_file=True,
            skip_quiz=True,
            privacy=True,
        ),
    )

    builder = agents.builder
    assert isinstance(builder, Scripted)
    assert any("Code Review Findings" in said for said in builder.heard)
    assert "[P1] undo returns a number" in "\n".join(builder.heard)
    # The round the findings opened is round 1, and the finalize round follows it.
    assert wrote == ["round-0", "round-1", "finalize"]


@pytest.mark.timeout(120)
def test_a_review_with_no_verdict_is_sent_back_for_one(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it the loop cannot tell an advancing round from a stalled one, so it refuses."""
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    reviews = {"at": 0}

    def reviewing(prompt: str) -> str:
        if prompt.startswith("You are a specialized agent that validates"):
            return _COMPLIES.format(why="it is this repository's")
        if prompt.startswith("# Code Review Phase"):
            return "Nothing to raise."
        reviews["at"] += 1
        if reviews["at"] == 1:
            return "Looks fine to me.\n\nCOMPLETE"  # no verdict line at all
        return "Holds.\n\nMainline Progress Verdict: ADVANCED\n\nCOMPLETE"

    agents = humanize1.Building(
        builder=Scripted(CONFIG, name="builder", doing=_builder(workspace, wrote)),
        reviewer=Scripted(CONFIG, name="reviewer", doing=reviewing),
        human=humanize1.HumanAgent(),
    )

    humanize1.rlcr(
        agents,
        "add undo",
        humanize1.Rlcr(
            plan_file="docs/plan.md",
            track_plan_file=True,
            skip_quiz=True,
            privacy=True,
        ),
    )

    builder = agents.builder
    assert isinstance(builder, Scripted)
    assert any("Mainline Verdict Missing" in said for said in builder.heard)


@pytest.mark.timeout(120)
def test_the_loop_stops_after_as_many_rounds_as_it_was_given(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--max`, which is the one thing that ends a loop nobody is ever going to accept."""
    monkeypatch.chdir(workspace)
    wrote: list[str] = []

    agents = humanize1.Building(
        builder=Scripted(CONFIG, name="builder", doing=_builder(workspace, wrote)),
        reviewer=Scripted(
            CONFIG,
            name="reviewer",
            doing=lambda said: (
                _COMPLIES.format(why="it is this repository's")
                if said.startswith("You are a specialized agent that validates")
                else "Still wrong.\n\nMainline Progress Verdict: ADVANCED\n"
            ),
        ),
        human=humanize1.HumanAgent(),
    )

    humanize1.rlcr(
        agents,
        "add undo",
        humanize1.Rlcr(
            plan_file="docs/plan.md",
            track_plan_file=True,
            skip_quiz=True,
            privacy=True,
            max=2,
        ),
    )

    (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
    assert (found / "maxiter-state.md").is_file()
    assert wrote == ["round-0", "round-1", "round-2"]


@pytest.mark.timeout(120)
def test_the_circuit_breaks_after_three_rounds_of_no_progress(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stalled twice is a recovery round; stalled three times is a loop to stop."""
    monkeypatch.chdir(workspace)
    wrote: list[str] = []

    agents = humanize1.Building(
        builder=Scripted(CONFIG, name="builder", doing=_builder(workspace, wrote)),
        reviewer=Scripted(
            CONFIG,
            name="reviewer",
            doing=lambda said: (
                _COMPLIES.format(why="it is this repository's")
                if said.startswith("You are a specialized agent that validates")
                else "Nothing moved.\n\nMainline Progress Verdict: STALLED\n"
            ),
        ),
        human=humanize1.HumanAgent(),
    )

    humanize1.rlcr(
        agents,
        "add undo",
        humanize1.Rlcr(
            plan_file="docs/plan.md",
            track_plan_file=True,
            skip_quiz=True,
            privacy=True,
        ),
    )

    builder = agents.builder
    assert isinstance(builder, Scripted)
    # The second stall asks for a recovery round, and the third breaks the circuit.
    assert any("Drift Recovery Mode" in said for said in builder.heard)
    assert any("Mainline Drift Circuit Breaker" in said for said in builder.heard)
    (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
    assert (found / "stop-state.md").is_file()


@pytest.mark.timeout(120)
def test_the_methodology_analysis_is_the_way_out_unless_privacy_says_otherwise(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop exits through it, and the state file is not renamed until it is done."""
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    building = _builder(workspace, wrote)

    def turn(prompt: str) -> str:
        if "Methodology Analysis Phase" in prompt:
            (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
            (found / "methodology-analysis-report.md").write_text(
                "rounds were productive\n"
            )
            (found / "methodology-analysis-done.md").write_text("analysis complete\n")
            wrote.append("methodology")
            return "analysed"
        return building(prompt)

    agents = humanize1.Building(
        builder=Scripted(CONFIG, name="builder", doing=turn),
        reviewer=Scripted(CONFIG, name="reviewer", doing=_reviewer([])),
        human=humanize1.HumanAgent(),
    )

    humanize1.rlcr(
        agents,
        "add undo",
        humanize1.Rlcr(
            plan_file="docs/plan.md",
            track_plan_file=True,
            skip_quiz=True,
        ),
    )

    (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
    assert wrote == ["round-0", "finalize", "methodology"]
    assert (found / "complete-state.md").is_file()
    assert (found / "methodology-analysis-report.md").is_file()


@pytest.mark.timeout(120)
def test_skip_impl_goes_straight_to_the_code_review(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No plan, no implementation phase: the branch is reviewed as it stands."""
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    asked: list[str] = []

    agents = humanize1.Building(
        builder=Scripted(CONFIG, name="builder", doing=_builder(workspace, wrote)),
        reviewer=Scripted(CONFIG, name="reviewer", doing=_reviewer(asked)),
        human=humanize1.HumanAgent(),
    )

    humanize1.rlcr(
        agents,
        "review what is here",
        humanize1.Rlcr(skip_impl=True, privacy=True),
    )

    # No summary review at all: the loop starts in the review phase, as `--skip-impl` says.
    assert asked == ["code-review"]  # nothing else: no plan, so nothing to check it
    (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
    assert (found / "complete-state.md").is_file()
    assert loop.State().bitlesson_required  # the default, which skip-impl turns off
    assert "bitlesson_required: false" in (found / "complete-state.md").read_text()


def test_a_plan_git_is_holding_is_refused_unless_the_run_said_so(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's rule: a plan is either in git and declared to be, or it is gitignored."""
    monkeypatch.chdir(workspace)
    running = loop.Loop(
        None,  # pyright: ignore[reportArgumentType]
        workspace / ".humanize" / "rlcr" / "now",
        workspace,
        loop.State(plan_file="docs/plan.md", plan_tracked=False, start_branch="main"),
    )

    from _humanize1.guards import Prompted

    refused = Prompted(running, workspace)(
        Occasion(moment=Moment.USER_PROMPT_SUBMIT, agent="builder")
    )

    assert refused is not None
    assert "now tracked in git" in refused.because


@pytest.mark.timeout(120)
def test_a_plan_that_leaves_git_mid_loop_is_refused_too(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop told the plan is in git is a loop whose integrity check needs it to stay there."""
    monkeypatch.chdir(workspace)
    running = loop.Loop(
        None,  # pyright: ignore[reportArgumentType]
        workspace / ".humanize" / "rlcr" / "now",
        workspace,
        loop.State(plan_file="docs/plan.md", plan_tracked=True, start_branch="main"),
    )
    _git("rm", "--cached", "docs/plan.md", at=workspace)

    from _humanize1.guards import Prompted

    refused = Prompted(running, workspace)(
        Occasion(moment=Moment.USER_PROMPT_SUBMIT, agent="builder")
    )

    assert refused is not None
    assert "no longer tracked in git" in refused.because


def _gated(workspace: Path, held: str) -> tuple[loop.Loop, Verdict | None]:
    """Puts one state file in front of the gate that reads it back, and answers with both.

    Args:
      workspace: The repository the loop is anchored to.
      held: What `state.md` says.

    Returns:
      The loop and whatever the gate said about it.
    """
    where = workspace / ".humanize" / "rlcr" / "now"
    where.mkdir(parents=True)
    (where / "state.md").write_text(held, encoding="utf-8")
    running = loop.Loop(
        None,  # pyright: ignore[reportArgumentType]
        where,
        workspace,
        loop.State(current_round=1, max_iterations=42),
    )
    return running, running._schema(Occasion(moment=Moment.STOP, agent="builder"))


def test_the_state_the_builder_may_not_write_is_read_back_each_round(
    workspace: Path,
) -> None:
    """Which is what the refusal to let it write the file is worth having for.

    The loop keeps the round in memory and writes it out, so on an ordinary round this reads
    back what it just wrote. On a round where something else got at the file, what is on disk
    is what the loop goes on from -- that is the whole point of looking.
    """
    running, said = _gated(
        workspace, loop.State(current_round=7, max_iterations=9).written()
    )

    assert said is None  # nothing to refuse: the file reads
    assert not running.over
    assert (running.state.current_round, running.state.max_iterations) == (7, 9)


def test_a_state_file_that_no_longer_says_which_round_it_is_ends_the_loop(
    workspace: Path,
) -> None:
    """A round number nothing can trust is a loop that stops rather than one that guesses."""
    written = loop.State(current_round=7).written()
    without = "\n".join(
        line for line in written.splitlines() if not line.startswith("current_round:")
    )

    running, said = _gated(workspace, without)

    assert said is None
    assert running.over == "unexpected"
    assert (running.where / "unexpected-state.md").is_file()


def test_a_round_number_that_is_not_a_number_is_left_as_it_was(workspace: Path) -> None:
    """The field is there and says something else, which is not the same as being gone."""
    written = loop.State(current_round=7, max_iterations=9).written()

    running, said = _gated(
        workspace, written.replace("current_round: 7", "current_round: x")
    )

    assert said is None
    assert not running.over
    assert running.state.current_round == 1  # as the loop had it, rather than zero
    assert running.state.max_iterations == 9  # and the field beside it still read


# ------------------------------------------------------------------------------------
# Picking a loop up where the last run of it stopped
# ------------------------------------------------------------------------------------


def _walks_off(building: Callable[[str], str], after: int) -> Callable[[str], str]:
    """A builder that walks off mid-loop, which is what a run there is anything to pick up is.

    Args:
      building: What a turn of it does while it is still working.
      after: How many turns it takes before it goes.

    Returns:
      What one turn of it does. The turn after the last is `Stopped`, which is a turn that
      does not happen rather than one that failed: the loop sends a failed turn round again,
      and this is the machine going down.
    """
    turns = {"at": 0}

    def turn(prompt: str) -> str:
        turns["at"] += 1
        if turns["at"] > after:
            raise Stopped
        return building(prompt)

    return turn


def _stalling(said: list[str]) -> Callable[[str], str]:
    """A reviewer that never accepts the work, so there is always another round to be on.

    Args:
      said: Filled in with what it was asked about, in order.

    Returns:
      What one turn of it does.
    """

    def turn(prompt: str) -> str:
        if prompt.startswith("You are a specialized agent that validates"):
            said.append("compliance")
            return _COMPLIES.format(why="it is this repository's")
        if prompt.startswith("You are a specialized agent that analyzes"):
            said.append("quiz")
            return "I would rather not"  # not a quiz, so the run goes on without one
        said.append("summary-review")
        return "There is more to do.\n\nMainline Progress Verdict: ADVANCED\n"

    return turn


def _building(builder: AgentBase, reviewer: AgentBase) -> humanize1.Building:
    """The three the loop is driven by, for a run that is about the two that work."""
    return humanize1.Building(
        builder=builder, reviewer=reviewer, human=humanize1.HumanAgent()
    )


@pytest.mark.timeout(120)
def test_a_stopped_loop_is_carried_on_in_the_directory_it_left(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which is the whole of what picking one up means: the same loop, at the round it reached.

    A loop stopped rather than finished is the ordinary case -- a machine went down, somebody
    pressed esc -- and what it leaves is a directory with the plan copied into it, the rounds
    it has been through and a state file saying where it got to. A second run that stamped a
    new directory would replan a week of work beside the week it already did.
    """
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    # One builder across both runs, so that each round writes something of its own: two runs
    # that wrote the same file would be a second round with nothing in it to commit.
    building = _builder(workspace, wrote)
    config = humanize1.Rlcr(
        plan_file="docs/plan.md", track_plan_file=True, privacy=True
    )
    first: list[str] = []
    kept: dict[str, Any] = {}

    with pytest.raises(Stopped):
        humanize1.rlcr(
            _building(
                Scripted(CONFIG, name="builder", doing=_walks_off(building, 1)),
                Scripted(CONFIG, name="reviewer", doing=_stalling(first)),
            ),
            "add undo",
            config,
            kept,
        )

    (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
    told = (found / "round-1-prompt.md").read_text()
    anchor = loop.State.read(found / "state.md")
    assert anchor is not None
    # What it kept: where the loop is, and how far it got. Everything else about the loop is
    # in that directory already, in the format the plugin's own tooling reads.
    assert kept == {"loop": f".humanize/rlcr/{found.name}", "rounds": 1}

    again: list[str] = []
    builder = Scripted(CONFIG, name="builder", doing=_walks_off(building, 1))
    with pytest.raises(Stopped):
        humanize1.rlcr(
            _building(
                builder, Scripted(CONFIG, name="reviewer", doing=_stalling(again))
            ),
            "add undo",
            config,
            kept,
        )

    # The same directory, with no second one beside it.
    assert sorted((workspace / ".humanize" / "rlcr").iterdir()) == [found]
    # And the builder -- a session that has just been opened -- picked up on what the loop
    # last wrote down for the round it was on, word for word.
    assert builder.heard[0] == told
    # The setup that had already happened did not happen twice: no compliance check, no quiz.
    assert first == ["compliance", "quiz", "summary-review"]
    assert again == ["summary-review"]
    # The anchor is where it was rather than where the repository has since got to: the loop
    # has judged every round of itself against that commit, and moving it moves all of them.
    state = loop.State.read(found / "state.md")
    assert state is not None
    assert (state.base_commit, state.start_branch) == (anchor.base_commit, "main")
    assert state.base_commit != loop.git("rev-parse", "HEAD", at=workspace)[1]
    # And the round went on from where it was, rather than back to zero.
    assert (state.current_round, kept["rounds"]) == (2, 2)
    assert wrote == ["round-0", "round-1"]


#: What the state of a loop a stopped run left behind says: four rounds into building the
#: plan this repository has committed, and set up the way the runs below are set up -- since
#: a loop is only carried on into a run that was set up its way, and what the tests using
#: this are about is the other reasons.
_LEFT_OFF: dict[str, Any] = {
    "current_round": 4,
    "plan_file": "docs/plan.md",
    "plan_tracked": True,
    "start_branch": "main",
    "privacy_mode": True,
}


def _left_off(
    workspace: Path, named: str = "2020-01-01_00-00-00", **fields: Any
) -> Path:
    """A loop directory as a run that was stopped mid-round leaves one behind.

    Args:
      workspace: The repository the loop is anchored to.
      named: What the directory is called, since a workspace may hold more than one.
      fields: What its state says over `_LEFT_OFF`.

    Returns:
      The directory.
    """
    where = workspace / ".humanize" / "rlcr" / named
    where.mkdir(parents=True)
    state = loop.State(**{**_LEFT_OFF, **fields})
    at = state.current_round
    (where / loop.BUILDING).write_text(state.written())
    if state.review_started:
        (where / loop.REVIEW_STARTED).write_text(f"build_finish_round={at}\n")
    shutil.copyfile(workspace / "docs" / "plan.md", where / "plan.md")
    (where / f"round-{at}-prompt.md").write_text(f"carry on with round {at}")
    return where


def _ended(work: Path, where: Path) -> None:
    """A loop that ran to the end, which renames its state file on the way out."""
    del work
    (where / "state.md").rename(where / "complete-state.md")


def _rewritten(work: Path, where: Path) -> None:
    """A loop whose state file holds a field this version of the flow has never had."""
    del work
    at = where / "state.md"
    at.write_text(at.read_text().replace("---\n", "---\nrung: 3\n", 1))


def _gone(work: Path, where: Path) -> None:
    """A loop somebody tidied away, leaving a state that names a directory that is not there."""
    del work
    shutil.rmtree(where)


def _replanned(work: Path, where: Path) -> None:
    """A plan changed since, which is what the plugin tells you to do about a wrong one.

    Stop the flow, edit the plan, start it again -- which has always meant a loop of its own:
    the loop that was running has judged every round of itself against the old plan, and its
    own integrity gate would refuse every round of it against the new one.
    """
    del where
    (work / "docs" / "plan.md").write_text(f"{PLAN}- AC-2: `redo()` puts it back.\n")
    _git("commit", "-am", "the plan, revised", at=work)


def _elsewhere(work: Path, where: Path) -> None:
    """The work moved to another branch, which is the first thing a round is refused for."""
    del where
    _git("checkout", "-b", "elsewhere", at=work)


def _untracked(work: Path, where: Path) -> None:
    """A loop set up with the plan gitignored, in a repository where it is now committed."""
    del work
    at = where / "state.md"
    at.write_text(at.read_text().replace("plan_tracked: true", "plan_tracked: false"))


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("spoil", "because"),
    [
        (_ended, "no live state file to carry on from"),
        (_rewritten, "no live state file to carry on from"),
        (_gone, "no live state file to carry on from"),
        (_replanned, "docs/plan.md has changed since that loop was set up"),
        (_elsewhere, "that loop is building on main, and this is on elsewhere"),
        (_untracked, "docs/plan.md is now tracked in git"),
    ],
)
def test_a_loop_there_is_no_carrying_on_is_a_fresh_one_and_says_so(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spoil: Callable[[Path, Path], None],
    because: str,
) -> None:
    """A loop is fifteen gates deep in what it reads, so half a resume is worse than none."""
    monkeypatch.chdir(workspace)
    stale = _left_off(workspace)
    spoil(workspace, stale)
    kept: dict[str, Any] = {"loop": f".humanize/rlcr/{stale.name}", "rounds": 4}
    builder = Scripted(CONFIG, name="builder", doing=_walks_off(lambda _: "", 0))

    with pytest.raises(Stopped):
        humanize1.rlcr(
            _building(builder, Scripted(CONFIG, name="reviewer", doing=_stalling([]))),
            "add undo",
            humanize1.Rlcr(
                plan_file="docs/plan.md",
                track_plan_file=True,
                skip_quiz=True,
                privacy=True,
            ),
            kept,
        )

    where = workspace / str(kept["loop"])
    assert where != stale
    assert kept["rounds"] == 0
    # Set up from nothing, and the builder started on round zero rather than on round four.
    assert builder.heard[0] == (where / "round-0-prompt.md").read_text()
    # And said which of the things a loop is carried on by was not there to carry it.
    said = capsys.readouterr().out
    assert because in said
    assert "Starting a fresh loop." in said


#: What a run of the tests below is set up with, which is what the loop `_left_off` leaves
#: was set up with too: a run only carries a loop on where the two agree.
_AS_SET_UP: dict[str, Any] = {
    "plan_file": "docs/plan.md",
    "track_plan_file": True,
    "skip_quiz": True,
    "privacy": True,
}


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("was", "says", "because"),
    [
        ({}, {"max": 9}, "that loop was set up with max 42, and this run says 9"),
        (
            {"agent_teams": True},
            {},
            "that loop was set up with agent_teams on, and this run says off",
        ),
        (
            {"push_every_round": True},
            {},
            "that loop was set up with push_every_round on, and this run says off",
        ),
        (
            {"codex_timeout": 60},
            {},
            "that loop was set up with codex_timeout 60, and this run says 5400",
        ),
        (
            {},
            {"plan_file": "docs/other.md"},
            "that loop is building docs/plan.md, and this run says docs/other.md",
        ),
        # A loop set up review-only, which is what a state saying the BitLesson entry is not
        # required is: this run means to build the plan, and that loop never will.
        (
            {"review_started": True, "bitlesson_required": False},
            {},
            "that loop was set up with skip_impl, and this run says it builds the plan",
        ),
    ],
)
def test_a_run_set_up_another_way_starts_a_loop_of_its_own_and_says_so(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    was: dict[str, Any],
    says: dict[str, Any],
    because: str,
) -> None:
    """Carrying a loop on means carrying its settings on: the rounds behind it ran by those.

    A run that says otherwise is not a run that quietly wins and not one that is quietly
    ignored either. Ignored is the worse of the two: the loop would go on doing what nobody
    asked it for this time -- pushing every round, leading a team of agents -- past the very
    checks at the top of the run that are there to say whether it may.
    """
    monkeypatch.chdir(workspace)
    # A second plan for the run that names one, committed as the first one is: what a run
    # pointed at a plan that is not there does is the test below this.
    (workspace / "docs" / "other.md").write_text(PLAN.replace("undo", "redo"))
    _git("add", "docs/other.md", at=workspace)
    _git("commit", "-m", "another plan", at=workspace)
    stale = _left_off(workspace, **was)
    kept: dict[str, Any] = {"loop": f".humanize/rlcr/{stale.name}", "rounds": 4}
    builder = Scripted(CONFIG, name="builder", doing=_walks_off(lambda _: "", 0))

    with pytest.raises(Stopped):
        humanize1.rlcr(
            _building(builder, Scripted(CONFIG, name="reviewer", doing=_stalling([]))),
            "add undo",
            humanize1.Rlcr(**{**_AS_SET_UP, **says}),
            kept,
        )

    where = workspace / str(kept["loop"])
    assert where != stale
    assert builder.heard[0] == (where / "round-0-prompt.md").read_text()
    said = capsys.readouterr().out
    assert because in said
    assert "Starting a fresh loop." in said
    # And the loop that is running is the one this run asked for, which is what the checks
    # at the top of the run were run against.
    fresh = loop.State.read(where / loop.BUILDING)
    assert fresh is not None
    assert (fresh.agent_teams, fresh.push_every_round) == (False, False)
    assert (fresh.max_iterations, fresh.codex_timeout) == (says.get("max", 42), 5400)


@pytest.mark.timeout(120)
def test_a_run_pointed_at_a_plan_that_is_not_there_says_so_rather_than_carrying_on(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first thing a run of this is checked for, and a resume is a run like any other."""
    monkeypatch.chdir(workspace)
    stale = _left_off(workspace)
    kept: dict[str, Any] = {"loop": f".humanize/rlcr/{stale.name}", "rounds": 4}
    builder = Scripted(CONFIG, name="builder", doing=_walks_off(lambda _: "", 0))

    with pytest.raises(ValueError, match="no plan file to build"):
        humanize1.rlcr(
            _building(builder, Scripted(CONFIG, name="reviewer", doing=_stalling([]))),
            "add undo",
            # As `_AS_SET_UP`, pointed at a plan this repository does not have.
            humanize1.Rlcr(
                plan_file="docs/nope.md",
                track_plan_file=True,
                skip_quiz=True,
                privacy=True,
            ),
            kept,
        )

    assert builder.heard == []  # nothing was sent a builder back in with


@pytest.mark.timeout(120)
def test_the_state_names_the_reviewer_that_is_reading_the_rounds_now(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agents are chosen per run, so a loop carried on says who is reading it now.

    `state.md` is what `humanize monitor rlcr` shows of a loop, and a resumed loop that went
    on naming the last run's reviewer would name a model nothing in this run is running.
    """
    monkeypatch.chdir(workspace)
    stale = _left_off(workspace, codex_model="gpt-5.6-sol", codex_effort="low")
    kept: dict[str, Any] = {"loop": f".humanize/rlcr/{stale.name}", "rounds": 4}

    with pytest.raises(Stopped):
        humanize1.rlcr(
            _building(
                Scripted(CONFIG, name="builder", doing=_walks_off(lambda _: "", 0)),
                Scripted(CONFIG, name="reviewer", doing=_stalling([])),
            ),
            "add undo",
            humanize1.Rlcr(**_AS_SET_UP),
            kept,
        )

    assert workspace / str(kept["loop"]) == stale  # the same loop, carried on
    state = loop.State.read(stale / loop.BUILDING)
    assert state is not None
    assert (state.codex_model, state.codex_effort) == (CONFIG.model, CONFIG.effort)


def _committed(work: Path, where: Path) -> None:
    """The plan put into git since, which the loop was told it was not in."""
    # Nothing to do: the fixture commits the plan, and the loop above is told it is not.
    del work, where


def _deleted(work: Path, where: Path) -> None:
    """The plan taken away since, which is the file the loop was set up from."""
    del where
    _git("rm", "--quiet", "docs/plan.md", at=work)
    _git("commit", "-m", "the plan, gone", at=work)


@pytest.mark.timeout(120)
def test_a_loop_in_the_code_review_is_not_thrown_away_over_the_plan(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past the implementation phase the plan is out of it, and this reads it the same way.

    The running loop's own `_plan_integrity` bails the moment the code review starts: the
    review reads the repository rather than the plan, so a plan that has changed since, or
    joined git, or left it, is nothing a round is judged by any more. A resume that threw the
    loop away over one would throw away a week of rounds for a file nothing is going to read.

    In the implementation phase, where a round is still judged against the plan, the same
    repository is a fresh loop -- which is what `_untracked` and `_replanned` above are.
    """
    monkeypatch.chdir(workspace)
    reviewing = _left_off(workspace, review_started=True, plan_tracked=False)
    _committed(workspace, reviewing)
    kept: dict[str, Any] = {"loop": f".humanize/rlcr/{reviewing.name}", "rounds": 4}

    carrying = humanize1._again(
        Scripted(CONFIG, name="reviewer", doing=lambda _: ""),
        humanize1.Rlcr(plan_file="docs/plan.md", skip_quiz=True, privacy=True),
        workspace,
        workspace / "docs" / "plan.md",
        kept,
    )

    assert carrying is not None
    running, told = carrying
    assert running.where == reviewing
    assert told == "carry on with round 4"
    assert "Starting a fresh loop." not in capsys.readouterr().out


@pytest.mark.timeout(120)
def test_a_loop_whose_plan_has_gone_is_not_carried_on_even_in_the_code_review(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plan being there at all is read in every phase, because the loop's guard reads it.

    A plan that has changed is out of it once the code review starts; a plan that is not
    there is not, and the difference is what happens to the run either way. `Prompted` reads
    the plan before every turn, in every phase, and refuses the one it cannot find -- so a
    loop carried on without it is a run whose first turn never happens, that takes no round,
    and that ends without a word about why. Said here instead: this loop is not carried on,
    and setting a fresh one up then says there is no plan to build, which is the truth.
    """
    monkeypatch.chdir(workspace)
    reviewing = _left_off(workspace, review_started=True, plan_tracked=False)
    _deleted(workspace, reviewing)
    kept: dict[str, Any] = {"loop": f".humanize/rlcr/{reviewing.name}", "rounds": 4}

    carrying = humanize1._again(
        Scripted(CONFIG, name="reviewer", doing=lambda _: ""),
        humanize1.Rlcr(plan_file="docs/plan.md", skip_quiz=True, privacy=True),
        workspace,
        workspace / "docs" / "plan.md",
        kept,
    )

    assert carrying is None
    said = capsys.readouterr().out
    said = said.replace(f"{workspace}/", "")
    assert "the plan that loop is building is not at docs/plan.md any more" in said
    assert "Starting a fresh loop." in said


@pytest.mark.timeout(120)
def test_a_loop_stopped_in_the_finalize_round_carries_on_in_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The phase renames the state file, which is where the loop is rather than the end of it.

    A loop that has passed its code review is one round from done. Reading `state.md` alone
    would find it gone, call the loop ended and plan the whole of the work again beside it.
    """
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    config = humanize1.Rlcr(**_AS_SET_UP)
    kept: dict[str, Any] = {}

    with pytest.raises(Stopped):
        humanize1.rlcr(
            _building(
                Scripted(
                    CONFIG,
                    name="builder",
                    doing=_walks_off(_builder(workspace, wrote), 1),
                ),
                Scripted(CONFIG, name="reviewer", doing=_reviewer([])),
            ),
            "add undo",
            config,
            kept,
        )

    (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
    assert (found / loop.FINALIZING).is_file()  # where the run was when it stopped
    assert wrote == ["round-0"]

    def finishing(prompt: str) -> str:
        del prompt
        (found / "finalize-summary.md").write_text("# Finalize\n\nsimplified nothing\n")
        wrote.append("finalize")
        return "finalized"

    builder = Scripted(CONFIG, name="builder", doing=_walks_off(finishing, 1))
    humanize1.rlcr(
        _building(builder, Scripted(CONFIG, name="reviewer", doing=_reviewer([]))),
        "add undo",
        config,
        kept,
    )

    # The same loop, one round from the end, rather than a second one planned beside it.
    assert sorted((workspace / ".humanize" / "rlcr").iterdir()) == [found]
    assert builder.heard[0] == (found / "finalize-prompt.md").read_text()
    assert "Finalize Phase" in builder.heard[0]
    assert wrote == ["round-0", "finalize"]
    assert (found / "complete-state.md").is_file()
    assert "in the finalize round" in capsys.readouterr().out


@pytest.mark.timeout(120)
def test_a_loop_stopped_in_the_methodology_analysis_carries_on_in_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The phase the loop exits through renames it too, and it is still a loop to finish."""
    monkeypatch.chdir(workspace)
    wrote: list[str] = []
    # As `_AS_SET_UP`, less the privacy that skips the analysis this test is about.
    config = humanize1.Rlcr(
        plan_file="docs/plan.md", track_plan_file=True, skip_quiz=True
    )
    kept: dict[str, Any] = {}

    with pytest.raises(Stopped):
        humanize1.rlcr(
            _building(
                Scripted(
                    CONFIG,
                    name="builder",
                    doing=_walks_off(_builder(workspace, wrote), 2),
                ),
                Scripted(CONFIG, name="reviewer", doing=_reviewer([])),
            ),
            "add undo",
            config,
            kept,
        )

    (found,) = sorted((workspace / ".humanize" / "rlcr").iterdir())
    assert (found / loop.ANALYSING).is_file()
    assert wrote == ["round-0", "finalize"]

    def analysing(prompt: str) -> str:
        del prompt
        (found / "methodology-analysis-report.md").write_text("rounds were productive")
        (found / "methodology-analysis-done.md").write_text("analysis complete\n")
        wrote.append("methodology")
        return "analysed"

    builder = Scripted(CONFIG, name="builder", doing=_walks_off(analysing, 1))
    humanize1.rlcr(
        _building(builder, Scripted(CONFIG, name="reviewer", doing=_reviewer([]))),
        "add undo",
        config,
        kept,
    )

    assert sorted((workspace / ".humanize" / "rlcr").iterdir()) == [found]
    assert builder.heard[0] == (found / "methodology-analysis-prompt.md").read_text()
    assert "Methodology Analysis Phase" in builder.heard[0]
    assert wrote == ["round-0", "finalize", "methodology"]
    # And it ended as what it was exiting for, which the phase was entered holding.
    assert (found / "complete-state.md").is_file()
    assert "in the methodology analysis" in capsys.readouterr().out
