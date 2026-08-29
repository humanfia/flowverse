"""The official plan flow is bounded and independent of either role's backend."""

# ruff: noqa: D103, PLR2004, S101

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from hmz.agents import AgentBase, AgentConfig, Event, Failed, SessionBase

ROOT = Path(__file__).parents[1]
FLOW = ROOT / "flows" / "humanize1"
sys.path[:0] = [str(FLOW), str(FLOW.parent)]

import humanize1  # noqa: E402

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterator

    from pydantic import BaseModel


CONFIG = AgentConfig(model="test-model", effort="high")
RELEVANT = json.dumps({"relevant": True, "why": "the draft belongs here"})
SETTLED = json.dumps(
    {
        "agree": ["the candidate is implementable"],
        "disagree": [],
        "required_changes": [],
        "optional_improvements": [],
        "unresolved": [],
    }
)
BLOCKED = json.dumps(
    {
        "agree": [],
        "disagree": [],
        "required_changes": ["name the exact compatibility contract"],
        "optional_improvements": [],
        "unresolved": [],
    }
)

CANDIDATE = """# Concrete Candidate

## Goal Description
Implement the bounded feature.

## Acceptance Criteria
- AC-1: The behavior is bounded.

## Path Boundaries
### Upper Bound (Maximum Acceptable Scope)
The complete bounded implementation.
### Lower Bound (Minimum Acceptable Scope)
The same observable behavior with fewer helpers.
### Allowed Choices
- Can use any backend through the Agent contract.

## Feasibility Hints and Suggestions
Use the existing flow-facing interfaces.

## Dependencies and Sequence
1. Add the contract.

## Task Breakdown
| Task ID | Description | Target AC | Tag (`coding`/`analyze`) | Depends On |
|---------|-------------|-----------|----------------------------|------------|
| task1 | Implement it | AC-1 | coding | - |

## Planner-Reviewer Deliberation
### Agreements
- The roles are backend-neutral.
### Resolved Disagreements
- None.
### Convergence Status
- Final Status: `converged` or `partially_converged`

## Pending User Decisions
- None.

## Implementation Notes
Keep role names independent of backend names.
"""

DECISION = """- DEC-1: Storage backend
  - Planner Position: keep it in sqlite
  - Reviewer Position: flat files are enough
  - Tradeoff Summary: durability against simplicity
  - Decision Status: {status}"""


class Scripted(AgentBase):
    """An agent whose role behavior is supplied by the test."""

    moments: ClassVar = frozenset()

    def __init__(
        self,
        name: str,
        doing: Callable[[str, ScriptedSession], str],
    ) -> None:
        super().__init__(CONFIG, name=name)
        self.doing = doing
        self.heard: list[str] = []

    def new(self, cwd: str | os.PathLike[str] | None = None) -> ScriptedSession:
        return ScriptedSession(self, cwd)


class ScriptedSession(SessionBase):
    """One fake conversation which can be released when its role is stopped."""

    def __init__(
        self, agent: AgentBase, cwd: str | os.PathLike[str] | None = None
    ) -> None:
        super().__init__(agent, cwd)
        self.released = threading.Event()

    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        del schema
        agent = self._agent
        assert isinstance(agent, Scripted)
        agent.heard.append(prompt)
        yield Event(kind="result", text=agent.doing(prompt, self))

    def _shut(self) -> None:
        self.released.set()


def _planner(
    plan: Path, *, revise_materially: bool = False, decisions: str = ""
) -> Scripted:
    def target(prompt: str) -> Path:
        for named in re.findall(r"/[^\s`]+", prompt):
            path = Path(named.rstrip(".,:;"))
            if ".humanize-plan-" in path.name:
                return path
        return plan

    def turn(prompt: str, _session: ScriptedSession) -> str:
        output = target(prompt)
        if "Candidate Plan v1" in prompt:
            held = output.read_text()
            appendix = (
                "\n--- Original Design Draft Start ---\n"
                + held.split("\n--- Original Design Draft Start ---\n", 1)[1]
            )
            output.write_text(CANDIDATE + appendix)
        elif "Revise the plan" in prompt and revise_materially:
            output.write_text(
                output.read_text().replace("bounded feature", "bounded public feature")
            )
        elif "finish the plan" in prompt:
            status = (
                "partially_converged"
                if "set to `partially_converged`" in prompt
                else "converged"
            )
            held = output.read_text().replace(
                "`converged` or `partially_converged`", f"`{status}`"
            )
            if decisions:
                held = held.replace(
                    "## Pending User Decisions\n- None.",
                    "## Pending User Decisions\n" + decisions,
                )
            output.write_text(held)
        return str(output)

    return Scripted("planner", turn)


def _analyst(review: str = SETTLED) -> Scripted:
    def turn(prompt: str, _session: ScriptedSession) -> str:
        if "determines whether" in prompt:
            return RELEVANT
        if "first planning pass" in prompt:
            return "CORE_RISKS:\n- none\n\nQUESTIONS_FOR_USER:\n- none"
        return review

    return Scripted("analyst", turn)


def _run(
    root: Path,
    planner: Scripted,
    analyst: Scripted,
    **configured: object,
) -> Path:
    draft = root / "draft.md"
    draft.write_text("A repository-specific draft.")
    output = root / "plan.md"
    settings = {
        "turn_timeout": 1,
        "total_timeout": 10,
        "turn_retries": 0,
    } | configured
    config = humanize1.Plan(
        input=str(draft),
        output=str(output),
        **settings,
    )
    humanize1.gen_plan(humanize1.Planning(planner, analyst), "make a plan", config)
    return output


def test_the_reviewer_receives_the_candidate_and_code_decides_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "plan.md"
    planner, analyst = _planner(output), _analyst()

    plan = _run(tmp_path, planner, analyst)

    (review,) = [
        prompt for prompt in analyst.heard if "complete candidate plan" in prompt
    ]
    assert "# Concrete Candidate" in review
    assert "Original Design Draft Start" not in review
    assert "Final Status: `converged`" in plan.read_text()


def test_permanent_review_failure_is_bounded_and_returns_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "plan.md"
    planner = _planner(output)
    calls = 0

    def failing(prompt: str, _session: ScriptedSession) -> str:
        nonlocal calls
        if "determines whether" in prompt:
            return RELEVANT
        calls += 1
        raise Failed(1, ["reviewer"], "", "service unavailable")

    plan = _run(
        tmp_path,
        planner,
        Scripted("reviewer", failing),
        turn_retries=1,
    )

    assert calls == 4  # two analysis attempts, then two review attempts
    assert "Final Status: `partially_converged`" in plan.read_text()


def test_a_review_timeout_stops_only_that_role_and_finishes_partial_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "plan.md"
    planner = _planner(output)

    def slow(prompt: str, session: ScriptedSession) -> str:
        if "determines whether" in prompt:
            return RELEVANT
        if "first planning pass" in prompt:
            return "CORE_RISKS:\n- none"
        session.released.wait(5)
        return SETTLED

    analyst = Scripted("reviewer", slow)
    began = time.monotonic()
    plan = _run(tmp_path, planner, analyst, turn_timeout=0.05)

    assert time.monotonic() - began < 1
    assert analyst.stopped
    assert not planner.stopped
    assert "Final Status: `partially_converged`" in plan.read_text()


def test_a_candidate_timeout_still_leaves_a_partial_plan_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "plan.md"
    normal = _planner(output)

    def slow_candidate(prompt: str, session: ScriptedSession) -> str:
        if "Candidate Plan v1" in prompt:
            session.released.wait(5)
        return normal.doing(prompt, session)

    planner = Scripted("planner", slow_candidate)
    plan = _run(tmp_path, planner, _analyst(), turn_timeout=0.05)

    assert plan.is_file()
    assert "Original Design Draft Start" in plan.read_text()
    assert "Final Status: `partially_converged`" in plan.read_text()


def test_a_late_planner_write_after_timeout_cannot_replace_the_durable_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def target(prompt: str) -> Path:
        return next(
            Path(named.rstrip(".,:;"))
            for named in re.findall(r"/[^\s`]+", prompt)
            if ".humanize-plan-" in Path(named.rstrip(".,:;")).name
        )

    def planner_turn(prompt: str, _session: ScriptedSession) -> str:
        staged = target(prompt)
        if "Candidate Plan v1" in prompt:
            held = staged.read_text()
            appendix = (
                "\n--- Original Design Draft Start ---\n"
                + held.split("\n--- Original Design Draft Start ---\n", 1)[1]
            )
            staged.write_text(CANDIDATE + appendix)
        elif "Revise the plan" in prompt:
            time.sleep(0.2)
            staged.write_text(
                staged.read_text().replace("bounded feature", "late mutation")
            )
        return str(staged)

    plan = _run(
        tmp_path,
        Scripted("planner", planner_turn),
        _analyst(BLOCKED),
        turn_timeout=0.05,
    )

    assert "late mutation" not in plan.read_text()
    assert "Final Status: `partially_converged`" in plan.read_text()


def test_two_non_material_revisions_stop_before_the_third_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "plan.md"
    planner, analyst = _planner(output), _analyst(BLOCKED)

    plan = _run(tmp_path, planner, analyst)

    reviews = [
        prompt for prompt in analyst.heard if "complete candidate plan" in prompt
    ]
    assert len(reviews) == 2
    assert "Final Status: `partially_converged`" in plan.read_text()


def test_convergence_rendering_keeps_the_original_five_sections() -> None:
    review = humanize1.Convergence.model_validate_json(BLOCKED)

    assert not review.converged
    assert review.rendered().splitlines()[0] == "AGREE:"
    assert (
        "REQUIRED_CHANGES:\n- name the exact compatibility contract"
        in review.rendered()
    )


def test_legacy_review_shape_is_parsed_instead_of_blindly_trusting_converged() -> None:
    review = humanize1.Convergence.model_validate_json(
        '{"converged": false, "review": "AGREE:\\n- fine\\n\\n'
        "DISAGREE:\\n- blocker\\n\\nREQUIRED_CHANGES:\\n- None\\n\\n"
        'OPTIONAL_IMPROVEMENTS:\\n- None\\n\\nUNRESOLVED:\\n- None"}'
    )

    assert not review.settled


def test_an_empty_review_cannot_converge_from_its_boolean_field() -> None:
    review = humanize1.Convergence(converged=True, review="")

    assert not review.settled


def test_default_planning_budgets_are_finite() -> None:
    config = humanize1.Plan()

    assert config.turn_timeout == 3600
    assert config.total_timeout == 14400
    assert config.turn_retries == 1


def test_a_decision_left_pending_stops_the_run_with_the_plan_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "plan.md"
    planner = _planner(output, decisions=DECISION.format(status="`PENDING`"))

    with pytest.raises(ValueError, match="PENDING.*DEC-1"):
        _run(tmp_path, planner, _analyst())

    held = output.read_text()
    assert "Planner Position: keep it in sqlite" in held
    assert "Final Status: `converged`" in held


def test_a_decision_answered_lets_the_plan_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "plan.md"
    planner = _planner(
        output, decisions=DECISION.format(status="sqlite, as the planner had it")
    )

    plan = _run(tmp_path, planner, _analyst())

    assert "Decision Status: sqlite, as the planner had it" in plan.read_text()


def test_the_templates_own_unfilled_status_line_counts_as_undecided() -> None:
    held = (
        "## Pending User Decisions\n\n"
        "- DEC-2: Cache eviction\n"
        "  - Decision Status: `PENDING` or `<User's final decision>`\n"
    )

    assert humanize1._undecided(held) == ["DEC-2"]  # noqa: SLF001
