from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from hmz.flows import (
    Budget,
    DurationExceeded,
    HarnessDropped,
    HarnessUnrecoverable,
    ModelUnavailable,
)
from hmz.runtime.flowing.environments import local_env
from hmz.runtime.flowing.fakes import FakeAgentDriver, run_fake

from tests.humanize1_kit import FLOW

sys.path[:0] = [str(FLOW), str(FLOW.parent)]

import humanize1  # noqa: E402

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


def _staged(prompt: str) -> Path | None:
    for named in re.findall(r"/[^\s`]+", prompt):
        path = Path(named.rstrip(".,:;"))
        if ".humanize-plan-" in path.name:
            return path
    return None


def _write_candidate(output: Path) -> None:
    held = output.read_text()
    appendix = (
        "\n--- Original Design Draft Start ---\n"
        + held.split("\n--- Original Design Draft Start ---\n", 1)[1]
    )
    output.write_text(CANDIDATE + appendix)


def _planning(
    plan: Path, *, revise_materially: bool = False, decisions: str = ""
) -> Any:
    def turn(prompt: str, **_: Any) -> str:
        output = _staged(prompt) or plan
        if "Candidate Plan v1" in prompt:
            _write_candidate(output)
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
        elif "Write a full translation" in prompt:
            variant = re.search(r"to (/\S+\.tmp)\.", prompt)
            assert variant is not None
            Path(variant.group(1)).write_text("译文\n" + plan.read_text())
        return str(output)

    return turn


def _planner(plan: Path, **said: Any) -> FakeAgentDriver:
    return FakeAgentDriver(reply=_planning(plan, **said))


def _analyst(review: str = SETTLED) -> FakeAgentDriver:
    def turn(prompt: str, **_: Any) -> str:
        if "determines whether" in prompt:
            return RELEVANT
        if "first planning pass" in prompt:
            return "CORE_RISKS:\n- none\n\nQUESTIONS_FOR_USER:\n- none"
        return review

    return FakeAgentDriver(reply=turn)


async def _run(
    root: Path,
    planner: FakeAgentDriver,
    analyst: FakeAgentDriver,
    **configured: Any,
) -> Path:
    draft = root / "draft.md"
    draft.write_text("A repository-specific draft.")
    output = root / "plan.md"
    settings = {
        "turn_timeout": 1,
        "total_timeout": 10,
        "turn_retries": 0,
    } | configured
    said = await run_fake(
        humanize1.gen_plan,
        "make a plan",
        agents={"planner": planner, "analyst": analyst},
        params=humanize1.Plan(input=str(draft), output=str(output), **settings),
        local=local_env(root),
    )
    assert said == str(output)
    return output


def _reviews(analyst: FakeAgentDriver) -> list[str]:
    return [prompt for prompt in analyst.prompts if "complete candidate plan" in prompt]


@pytest.mark.asyncio
async def test_the_reviewer_receives_the_candidate_and_code_decides_convergence(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan.md"
    planner, analyst = _planner(output), _analyst()

    plan = await _run(tmp_path, planner, analyst)

    (review,) = _reviews(analyst)
    assert "# Concrete Candidate" in review
    assert "Original Design Draft Start" not in review
    assert "Final Status: `converged`" in plan.read_text()
    assert not list(tmp_path.glob(".humanize-plan-*.tmp"))


@pytest.mark.asyncio
async def test_permanent_review_failure_is_bounded_and_returns_the_candidate(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan.md"
    planner = _planner(output)
    calls = 0

    def failing(prompt: str, **_: Any) -> str:
        nonlocal calls
        if "determines whether" in prompt:
            return RELEVANT
        calls += 1
        raise HarnessDropped("service unavailable")

    plan = await _run(tmp_path, planner, FakeAgentDriver(reply=failing), turn_retries=1)

    # The analysis and the first review, each tried twice; nothing more is asked of it.
    assert calls == 4
    assert "Final Status: `partially_converged`" in plan.read_text()


@pytest.mark.asyncio
async def test_an_unrecoverable_failure_is_not_retried(tmp_path: Path) -> None:
    output = tmp_path / "plan.md"
    calls = 0

    def unrecoverable(prompt: str, **_: Any) -> str:
        nonlocal calls
        if "determines whether" in prompt:
            return RELEVANT
        if "first planning pass" in prompt:
            calls += 1
            raise HarnessUnrecoverable("the account is gone")
        return SETTLED

    plan = await _run(
        tmp_path,
        _planner(output),
        FakeAgentDriver(reply=unrecoverable),
        turn_retries=3,
    )

    assert calls == 1
    assert "Final Status: `converged`" in plan.read_text()


@pytest.mark.asyncio
async def test_a_bad_structured_answer_is_retried(tmp_path: Path) -> None:
    output = tmp_path / "plan.md"
    answers = iter(["not a review at all", SETTLED])

    def turn(prompt: str, **_: Any) -> str:
        if "determines whether" in prompt:
            return RELEVANT
        if "first planning pass" in prompt:
            return "CORE_RISKS:\n- none"
        return next(answers)

    analyst = FakeAgentDriver(reply=turn)
    plan = await _run(tmp_path, _planner(output), analyst, turn_retries=1)

    assert len(_reviews(analyst)) == 2
    assert "Final Status: `converged`" in plan.read_text()


@pytest.mark.asyncio
async def test_a_review_timeout_stops_only_that_role_and_finishes_partial_plan(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan.md"
    planner = _planner(output)

    async def slow(prompt: str, **_: Any) -> str:
        if "determines whether" in prompt:
            return RELEVANT
        if "first planning pass" in prompt:
            return "CORE_RISKS:\n- none"
        await asyncio.sleep(5)
        return SETTLED

    analyst = FakeAgentDriver(reply=slow)
    began = time.monotonic()
    plan = await _run(tmp_path, planner, analyst, turn_timeout=0.05)

    assert time.monotonic() - began < 1
    assert len(_reviews(analyst)) == 1
    assert any("finish the plan" in prompt for prompt in planner.prompts)
    assert "Final Status: `partially_converged`" in plan.read_text()


@pytest.mark.asyncio
async def test_a_candidate_timeout_still_leaves_a_partial_plan_file(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan.md"
    normal = _planning(output)

    async def slow_candidate(prompt: str, **said: Any) -> str:
        if "Candidate Plan v1" in prompt:
            await asyncio.sleep(5)
        return normal(prompt, **said)

    planner = FakeAgentDriver(reply=slow_candidate)
    plan = await _run(tmp_path, planner, _analyst(), turn_timeout=0.05)

    assert plan.is_file()
    assert "Original Design Draft Start" in plan.read_text()
    assert "Final Status: `partially_converged`" in plan.read_text()
    assert len(planner.prompts) == 1
    assert not list(tmp_path.glob(".humanize-plan-*.tmp"))


@pytest.mark.asyncio
async def test_a_late_planner_write_after_timeout_cannot_replace_the_durable_plan(
    tmp_path: Path,
) -> None:
    lingering: list[asyncio.Task[None]] = []

    async def late(staged: Path) -> None:
        await asyncio.sleep(0.2)
        staged.write_text(CANDIDATE.replace("bounded feature", "late mutation"))

    async def planner_turn(prompt: str, **_: Any) -> str:
        staged = _staged(prompt)
        assert staged is not None
        if "Candidate Plan v1" in prompt:
            _write_candidate(staged)
        elif "Revise the plan" in prompt:
            # A CLI that goes on writing after its turn was given up on.
            lingering.append(asyncio.get_running_loop().create_task(late(staged)))
            await asyncio.sleep(5)
        return str(staged)

    plan = await _run(
        tmp_path,
        FakeAgentDriver(reply=planner_turn),
        _analyst(BLOCKED),
        turn_timeout=0.05,
    )
    await asyncio.gather(*lingering)

    assert len(lingering) == 1
    assert "late mutation" not in plan.read_text()
    assert "Final Status: `partially_converged`" in plan.read_text()


@pytest.mark.asyncio
async def test_two_non_material_revisions_stop_before_the_third_review(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan.md"
    planner, analyst = _planner(output), _analyst(BLOCKED)

    plan = await _run(tmp_path, planner, analyst)

    assert len(_reviews(analyst)) == 2
    assert "Final Status: `partially_converged`" in plan.read_text()


@pytest.mark.asyncio
async def test_material_revisions_keep_converging_up_to_three_reviews(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan.md"
    planner = _planner(output, revise_materially=True)
    analyst = _analyst(BLOCKED)

    plan = await _run(tmp_path, planner, analyst)

    assert len(_reviews(analyst)) == 3
    assert "bounded public feature" in plan.read_text()
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


@pytest.mark.asyncio
async def test_a_decision_left_pending_stops_the_run_with_the_plan_kept(
    tmp_path: Path,
) -> None:
    output = tmp_path / "plan.md"
    planner = _planner(output, decisions=DECISION.format(status="`PENDING`"))

    with pytest.raises(ValueError, match="PENDING.*DEC-1"):
        await _run(tmp_path, planner, _analyst())

    held = output.read_text()
    assert "Planner Position: keep it in sqlite" in held
    assert "Final Status: `converged`" in held


@pytest.mark.asyncio
async def test_a_decision_answered_lets_the_plan_finish(tmp_path: Path) -> None:
    output = tmp_path / "plan.md"
    planner = _planner(
        output, decisions=DECISION.format(status="sqlite, as the planner had it")
    )

    plan = await _run(tmp_path, planner, _analyst())

    assert "Decision Status: sqlite, as the planner had it" in plan.read_text()


def test_the_templates_own_unfilled_status_line_counts_as_undecided() -> None:
    held = (
        "## Pending User Decisions\n\n"
        "- DEC-2: Cache eviction\n"
        "  - Decision Status: `PENDING` or `<User's final decision>`\n"
    )

    assert humanize1._undecided(held) == ["DEC-2"]


@pytest.mark.asyncio
async def test_a_draft_about_something_else_is_refused(tmp_path: Path) -> None:
    unrelated = json.dumps({"relevant": False, "why": "it is a cake recipe"})

    with pytest.raises(ValueError, match="cake recipe"):
        await _run(
            tmp_path,
            _planner(tmp_path / "plan.md"),
            FakeAgentDriver(reply=unrelated),
        )
    assert not (tmp_path / "plan.md").exists()


@pytest.mark.asyncio
async def test_the_last_idea_is_planned_and_translated(tmp_path: Path) -> None:
    ideas = tmp_path / ".humanize" / "ideas"
    ideas.mkdir(parents=True)
    (ideas / "older.md").write_text("An older draft.")
    (ideas / "newer.md").write_text("The newer draft.")
    os.utime(ideas / "older.md", (1_000_000_000, 1_000_000_000))
    output = tmp_path / "docs" / "plan.md"
    planner, analyst = _planner(output), _analyst()

    said = await run_fake(
        humanize1.gen_plan,
        "make a plan",
        agents={"planner": planner, "analyst": analyst},
        params={"alternative_plan_language": "chinese", "turn_retries": 0},
        local=local_env(tmp_path),
    )

    assert said == str(output)
    assert "The newer draft." in output.read_text()
    assert "Final Status: `converged`" in output.read_text()
    assert (tmp_path / "docs" / "plan_zh.md").read_text().startswith("译文\n")
    assert not list((tmp_path / "docs").glob(".humanize-plan-*.tmp"))


@pytest.mark.asyncio
async def test_an_existing_plan_is_never_written_over(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("somebody's plan")

    with pytest.raises(ValueError, match="already exists"):
        await _run(tmp_path, _planner(tmp_path / "plan.md"), _analyst())
    assert (tmp_path / "plan.md").read_text() == "somebody's plan"


@pytest.mark.asyncio
async def test_the_callers_budget_running_out_is_not_a_turn_timeout(
    tmp_path: Path,
) -> None:
    async def slow(prompt: str, **_: Any) -> str:
        if "determines whether" in prompt:
            return RELEVANT
        await asyncio.sleep(0.5)
        return "CORE_RISKS:\n- none"

    draft = tmp_path / "draft.md"
    draft.write_text("A repository-specific draft.")
    with pytest.raises(DurationExceeded):
        await run_fake(
            humanize1.gen_plan,
            "make a plan",
            agents={
                "planner": _planner(tmp_path / "plan.md"),
                "analyst": FakeAgentDriver(reply=slow),
            },
            params=humanize1.Plan(
                input=str(draft), output=str(tmp_path / "plan.md"), turn_retries=0
            ),
            budget=Budget(duration=datetime.timedelta(seconds=0.2)),
            local=local_env(tmp_path),
        )


@pytest.mark.asyncio
async def test_a_model_nobody_serves_is_asked_once_and_leaves_a_partial_plan(
    tmp_path: Path,
) -> None:
    calls = 0

    def unserved(prompt: str, **_: Any) -> str:
        nonlocal calls
        calls += 1
        raise ModelUnavailable("no such model")

    plan = await _run(
        tmp_path,
        _planner(tmp_path / "plan.md"),
        FakeAgentDriver(reply=unserved),
        turn_retries=3,
    )

    assert calls == 1
    held = plan.read_text()
    assert "Final Status: `partially_converged`" in held
    assert "- Flow Note: draft relevance check: no such model" in held
    assert not list(tmp_path.glob(".humanize-plan-*.tmp"))


@pytest.mark.asyncio
async def test_a_planner_that_only_writes_the_file_is_enough(tmp_path: Path) -> None:
    output = tmp_path / "plan.md"
    writes = _planning(output)

    def silently(prompt: str, **said: Any) -> str:
        writes(prompt, **said)
        return ""

    plan = await _run(tmp_path, FakeAgentDriver(reply=silently), _analyst())

    assert "# Concrete Candidate" in plan.read_text()
    assert "Final Status: `converged`" in plan.read_text()


@pytest.mark.asyncio
async def test_a_crlf_draft_is_kept_whole(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_bytes(b"A repository-specific draft.\r\nWith two lines.\r\n")
    output = tmp_path / "plan.md"

    said = await run_fake(
        humanize1.gen_plan,
        "make a plan",
        agents={"planner": _planner(output), "analyst": _analyst()},
        params=humanize1.Plan(input=str(draft), output=str(output), turn_retries=0),
        local=local_env(tmp_path),
    )

    assert said == str(output)
    held = output.read_text()
    assert "Final Status: `converged`" in held
    assert held.endswith(
        "A repository-specific draft.\nWith two lines.\n"
        "\n--- Original Design Draft End ---\n"
    )
