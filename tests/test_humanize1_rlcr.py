from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest
from hmz.flows import (
    HarnessDropped,
    HarnessKilled,
    HarnessMissing,
    UserPromptSubmitHookParams,
)
from hmz.runtime.flowing.environments import local_env
from hmz.runtime.flowing.fakes import FakeAgentDriver, FakeOutworlder, run_fake

from tests.humanize1_kit import FLOW, git, repository

sys.path[:0] = [str(FLOW), str(FLOW.parent)]

import humanize1  # noqa: E402
from _humanize1 import guards  # noqa: E402
from _humanize1 import loop as looping  # noqa: E402
from _humanize1.loop import Loop, State  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from hmz.runtime.flowing.fakes import FakeSession

    Act = Callable[[FakeSession, Path, str], Awaitable[str | None]]

PLAN = """# Plan

## Goal
Add a feature module.

## Acceptance Criteria
- AC-1: feature.py exists and says which round wrote it.

## Tasks
- Write feature.py.
"""

SUMMARY = """# Round {n} Summary

## Work Completed
- Wrote feature.py.

## BitLesson Delta
- Action: none
- Lesson ID(s): NONE
- Notes: nothing new was learned
"""

PLACEHOLDER = "[To be populated by the builder based on plan]"

NOT_DONE = "The tests are missing.\n\nMainline Progress Verdict: ADVANCED\n"
DONE = "Everything the plan asks for is there.\n\nMainline Progress Verdict: ADVANCED\nCOMPLETE"
STALLED = "Nothing moved.\n\nMainline Progress Verdict: STALLED\n"
CLEAN = "No issues found.\n"
FINDING = "Review of the branch.\n\n[P1] feature.py lacks a docstring\n"

QUIZ = {
    "questions": [
        {
            "question": "What does the plan add?",
            "options": ["a feature module", "a database", "a CLI", "nothing"],
            "answer": "A",
        },
        {
            "question": "Which file says the round?",
            "options": ["README.md", "feature.py", "setup.py", "plan.md"],
            "answer": "B",
        },
    ],
    "summary": "The plan adds feature.py, written round by round.",
}


def _repo(tmp_path: Path, *, tracked: bool = False) -> Path:
    at = repository(tmp_path / "repo")
    (at / "docs").mkdir()
    (at / "docs" / "plan.md").write_text(PLAN)
    if tracked:
        git(at, "add", "docs/plan.md")
        git(at, "commit", "-m", "plan")
    else:
        (at / ".git" / "info" / "exclude").write_text("docs/plan.md\n")
    return at


def _loops(repo: Path) -> list[Path]:
    return sorted((repo / ".humanize" / "rlcr").iterdir())


def _round(loop: Path) -> int:
    found = re.search(r"^current_round: (\d+)$", (loop / "state.md").read_text(), re.M)
    assert found is not None
    return int(found.group(1))


class Builder:
    def __init__(self, repo: Path, acts: dict[int, Act] | None = None) -> None:
        self.repo = repo
        self.acts = acts or {}
        self.turns = 0

    async def __call__(self, prompt: str, *, session: FakeSession, **_: Any) -> str:
        turn = self.turns
        self.turns += 1
        (loop,) = _loops(self.repo)
        act = self.acts.get(turn)
        if act is not None and (said := await act(session, loop, prompt)) is not None:
            return said
        if prompt.startswith("# Methodology Analysis"):
            (loop / "methodology-analysis-report.md").write_text("what went well\n")
            (loop / "methodology-analysis-done.md").write_text("done\n")
            return "analysed"
        if prompt.startswith("# Finalize Phase"):
            (loop / "finalize-summary.md").write_text(
                "# Finalize Summary\nsimplified\n"
            )
            return "finalized"
        at = _round(loop)
        tracker = loop / "goal-tracker.md"
        tracker.write_text(tracker.read_text().replace(PLACEHOLDER, "Write feature.py"))
        (loop / f"round-{at}-summary.md").write_text(SUMMARY.format(n=at))
        (loop / f"round-{at}-contract.md").write_text(f"# Round {at} Contract\n")
        (self.repo / "feature.py").write_text(f"ROUND = {at}\nTURN = {turn}\n")
        git(self.repo, "add", "feature.py")
        git(self.repo, "commit", "-m", f"round {at}, turn {turn}")
        return f"round {at} done"


def _reviewer(*reviews: str, code: tuple[str, ...] = (CLEAN,)) -> FakeAgentDriver:
    said = iter(reviews)
    coded = iter(code)

    async def turn(prompt: str, *, output_schema: Any, **_: Any) -> Any:
        if output_schema is not None and output_schema.__name__ == "Compliance":
            return {"relevant": True, "switches_branch": False, "why": "a feature"}
        if output_schema is not None and output_schema.__name__ == "Quiz":
            return QUIZ
        if prompt.startswith("# Code Review Phase"):
            return next(coded)
        return next(said)

    return FakeAgentDriver(reply=turn)


async def _rlcr(
    repo: Path,
    builder: Builder | FakeAgentDriver,
    reviewer: FakeAgentDriver,
    **params: Any,
) -> Any:
    driver = FakeAgentDriver(reply=builder) if isinstance(builder, Builder) else builder
    outworlder = params.pop("outworlder", None)
    return await run_fake(
        humanize1.rlcr,
        "build the plan",
        agents={"builder": driver, "reviewer": reviewer},
        params=params,
        local=local_env(repo),
        outworlder=outworlder,
    )


@pytest.mark.asyncio
async def test_skip_code_review_overrides_automatic_main_detection(
    tmp_path: Path,
) -> None:
    env = local_env(repository(tmp_path))

    assert await humanize1._base(env, "") == "main"
    assert (
        await humanize1._review_base(
            env, humanize1.Rlcr(base_branch="", skip_code_review=True)
        )
        == ""
    )
    assert (
        await humanize1._review_base(
            env, humanize1.Rlcr(base_branch="", skip_code_review=False)
        )
        == "main"
    )
    assert await humanize1._base(env, "release") == "release"


@pytest.mark.asyncio
async def test_rounds_run_to_max_and_exit_through_the_methodology_analysis(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    reviewer = _reviewer(NOT_DONE, NOT_DONE)
    released: list[bool] = []

    async def looks(session: FakeSession, loop: Path, prompt: str) -> None:
        released.extend(one.closed for one in reviewer.sessions)

    builder = Builder(repo, {2: looks})

    over = await _rlcr(repo, builder, reviewer, max=2)

    assert over == "maxiter"
    (loop,) = _loops(repo)
    assert (loop / "maxiter-state.md").is_file()
    assert not (loop / "state.md").exists()
    assert (loop / "plan.md").read_text() == PLAN
    assert builder.turns == 4
    reviews = [p for p in reviewer.prompts if p.startswith("# Code Review - Round")]
    assert [p.splitlines()[0] for p in reviews] == [
        "# Code Review - Round 0",
        "# Code Review - Round 1",
    ]
    assert (loop / "round-0-review-result.md").read_text() == NOT_DONE
    assert (loop / "round-2-prompt.md").is_file()
    assert (
        (loop / "methodology-analysis-prompt.md")
        .read_text()
        .startswith("# Methodology Analysis")
    )
    assert "maxiter" in (loop / "methodology-analysis-prompt.md").read_text()
    assert git(repo, "log", "--format=%s", "-1") == "round 2, turn 2"
    # Every reviewer session is closed once nothing holds it: each review's went with the
    # review, and the compliance check's as soon as the loop let go of it.
    assert released == [True, True, True]


@pytest.mark.asyncio
async def test_the_reviewer_approving_goes_through_code_review_and_finalize(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    builder = Builder(repo)
    reviewer = _reviewer(NOT_DONE, DONE, code=(FINDING, CLEAN))

    over = await _rlcr(repo, builder, reviewer, privacy=True)

    assert over == "complete"
    (loop,) = _loops(repo)
    assert (loop / "complete-state.md").is_file()
    assert (loop / ".review-phase-started").read_text() == "build_finish_round=1\n"
    assert (loop / "round-2-review-result.md").read_text() == FINDING
    assert (
        "[P1] feature.py lacks a docstring" in (loop / "round-2-prompt.md").read_text()
    )
    assert (loop / "finalize-prompt.md").read_text().startswith("# Finalize Phase")
    assert builder.turns == 4
    compliance, *_ = reviewer.prompts
    assert "validates an implementation plan" in compliance


@pytest.mark.asyncio
async def test_skip_impl_goes_straight_to_the_code_review(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    builder = Builder(repo)
    reviewer = _reviewer(code=(CLEAN,))

    over = await _rlcr(repo, builder, reviewer, skip_impl=True, privacy=True)

    assert over == "complete"
    (loop,) = _loops(repo)
    assert (loop / "plan.md").read_text().startswith("# Skip Implementation Mode")
    assert [p.splitlines()[0] for p in reviewer.prompts] == [
        "# Code Review Phase - Round 1"
    ]


@pytest.mark.asyncio
async def test_open_tasks_hold_the_builder_until_they_are_done(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    async def plans(session: FakeSession, loop: Path, prompt: str) -> str:
        await session.tool(
            "TaskCreate", {"subject": "[mainline] write feature.py", "description": ""}
        )
        await session.tool("TaskCreate", {"subject": "[queued] polish the docs"})
        await session.tool(
            "TodoWrite",
            {"todos": [{"content": "run the tests", "status": "in_progress"}]},
        )
        return "stopping early"

    async def finishes(session: FakeSession, loop: Path, prompt: str) -> None:
        await session.tool("TaskUpdate", {"taskId": "1", "status": "completed"})
        await session.tool(
            "TodoWrite",
            {"todos": [{"content": "run the tests", "status": "completed"}]},
        )

    driver = FakeAgentDriver(reply=Builder(repo, {0: plans, 1: finishes}))

    over = await _rlcr(
        repo, driver, _reviewer(DONE), skip_code_review=True, privacy=True
    )

    assert over == "complete"
    held = driver.prompts
    assert held[1].startswith("# Incomplete Tasks Detected")
    assert "  - [pending] [mainline] (Task #1) [mainline] write feature.py" in held[1]
    assert "  - [in_progress] [blocking] run the tests" in held[1]
    assert "polish the docs" not in held[1]
    assert held[2].startswith("# Finalize Phase (Review Skipped)")
    assert len(held) == 3


@pytest.mark.asyncio
async def test_a_dirty_tree_and_a_missing_summary_are_sent_back(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    async def dirty(session: FakeSession, loop: Path, prompt: str) -> str:
        (repo / "feature.py").write_text("left uncommitted\n")
        return "stopping early"

    async def unsummarised(session: FakeSession, loop: Path, prompt: str) -> str:
        git(repo, "add", "feature.py")
        git(repo, "commit", "-m", "committed at last")
        (loop / "round-0-summary.md").write_text("   \n")
        return "stopping early again"

    builder = Builder(repo, {0: dirty, 1: unsummarised})
    driver = FakeAgentDriver(reply=builder)

    over = await _rlcr(
        repo, driver, _reviewer(DONE), skip_code_review=True, privacy=True
    )

    assert over == "complete"
    held = driver.prompts
    assert held[1].startswith("# Git Not Clean")
    assert held[2].startswith("# Work Summary Missing")


@pytest.mark.asyncio
async def test_the_guard_refuses_what_the_builder_must_not_touch(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    reached: list[tuple[str, bool]] = []

    async def tries(session: FakeSession, loop: Path, prompt: str) -> None:
        attempts: list[tuple[str, str, dict[str, Any]]] = [
            ("state", "Write", {"file_path": str(loop / "state.md")}),
            ("backup", "Write", {"file_path": str(loop / "plan.md")}),
            ("plan", "Edit", {"file_path": "docs/plan.md", "old_string": "Goal"}),
            ("prompt", "Write", {"file_path": str(loop / "round-0-prompt.md")}),
            ("later round", "Write", {"file_path": str(loop / "round-3-summary.md")}),
            ("summary elsewhere", "Write", {"file_path": "round-0-summary.md"}),
            ("todos", "Read", {"file_path": str(loop / "round-0-todos.md")}),
            ("push", "Bash", {"command": "git push origin main"}),
            ("codex push", "commandExecution", {"command": ["git", "push"]}),
            ("add all", "Bash", {"command": "git add -A && git commit -m x"}),
            (
                "tracker by bash",
                "Bash",
                {"command": f"echo x >> {loop}/goal-tracker.md"},
            ),
            ("state by sed", "Bash", {"command": f"sed -i s/0/9/ {loop}/state.md"}),
            ("summary", "Write", {"file_path": str(loop / "round-0-summary.md")}),
            ("tracker", "Edit", {"file_path": str(loop / "goal-tracker.md")}),
            ("code", "Write", {"file_path": "feature.py"}),
            ("status", "Bash", {"command": "git status --porcelain"}),
            ("read summary", "Read", {"file_path": str(loop / "round-0-summary.md")}),
        ]
        for label, tool, called in attempts:
            reached.append((label, await session.tool(tool, called)))

    over = await _rlcr(
        repo,
        Builder(repo, {0: tries}),
        _reviewer(DONE),
        skip_code_review=True,
        privacy=True,
    )

    assert over == "complete"
    assert reached == [
        ("state", False),
        ("backup", False),
        ("plan", False),
        ("prompt", False),
        ("later round", False),
        ("summary elsewhere", False),
        ("todos", False),
        ("push", False),
        ("codex push", False),
        ("add all", False),
        ("tracker by bash", False),
        ("state by sed", False),
        ("summary", True),
        ("tracker", True),
        ("code", True),
        ("status", True),
        ("read summary", True),
    ]


@pytest.mark.asyncio
async def test_a_tracked_plan_is_refused_before_anything_is_asked(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, tracked=True)
    driver = FakeAgentDriver(reply=Builder(repo))
    reviewer = _reviewer(DONE)

    with pytest.raises(ValueError, match="track_plan_file"):
        await _rlcr(repo, driver, reviewer)
    assert driver.prompts == []
    assert reviewer.prompts == []
    assert not (repo / ".humanize").exists()

    over = await _rlcr(
        repo,
        Builder(repo),
        _reviewer(DONE),
        track_plan_file=True,
        skip_code_review=True,
        privacy=True,
    )
    assert over == "complete"


@pytest.mark.asyncio
async def test_the_opening_prompt_is_refused_on_another_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    running = Loop(
        reviewer=None,  # pyright: ignore[reportArgumentType]
        env=local_env(repo),  # pyright: ignore[reportArgumentType]
        where=PurePosixPath(repo / ".humanize" / "rlcr" / "loop"),
        state=State(plan_file="docs/plan.md", start_branch="main"),
    )
    prompted = guards.Prompted(running)

    def submitted(prompt: str) -> UserPromptSubmitHookParams:
        return UserPromptSubmitHookParams(ctx=None, session=None, prompt=prompt)  # pyright: ignore[reportArgumentType]

    assert not (await prompted(submitted("start"))).block
    git(repo, "checkout", "-b", "elsewhere")
    said = await prompted(submitted("start"))
    assert said.block
    assert said.reason.startswith("Git branch changed during RLCR loop.")
    running.continuing = "Switch back to main."
    assert not (await prompted(submitted("Switch back to main."))).block


@pytest.mark.asyncio
async def test_a_base_branch_that_is_not_there_is_refused_up_front(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    reviewer = _reviewer(DONE)

    with pytest.raises(ValueError, match="'mian' names no commit"):
        await _rlcr(repo, Builder(repo), reviewer, base_branch="mian")
    assert reviewer.prompts == []


@pytest.mark.asyncio
async def test_a_code_review_that_says_nothing_is_not_a_clean_one(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    driver = FakeAgentDriver(reply=Builder(repo))

    over = await _rlcr(repo, driver, _reviewer(DONE, code=("", CLEAN)), privacy=True)

    assert over == "complete"
    failed = driver.prompts[1]
    assert failed.startswith("# Review Failed")
    assert "the code review answered nothing" in failed
    assert driver.prompts[2].startswith("# Finalize Phase")


@pytest.mark.asyncio
async def test_three_stalled_rounds_trip_the_drift_circuit_breaker(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    builder = Builder(repo)

    over = await _rlcr(
        repo, builder, _reviewer(STALLED, STALLED, STALLED), privacy=True
    )

    assert over == "stop"
    assert builder.turns == 3
    (loop,) = _loops(repo)
    assert (loop / "stop-state.md").is_file()
    assert (
        "Mainline Drift Circuit Breaker" not in (loop / "round-2-prompt.md").read_text()
    )


@pytest.mark.asyncio
async def test_a_review_that_fails_once_is_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(looping, "_PAUSE", 0)
    repo = _repo(tmp_path)
    answers: list[str | Exception] = [HarnessDropped("the connection broke"), DONE]

    async def flaky(prompt: str, *, output_schema: Any, **_: Any) -> Any:
        if output_schema is not None:
            return {"relevant": True, "switches_branch": False, "why": "a feature"}
        said = answers.pop(0)
        if isinstance(said, Exception):
            raise said
        return said

    driver = FakeAgentDriver(reply=Builder(repo))
    over = await _rlcr(
        repo,
        driver,
        FakeAgentDriver(reply=flaky),
        skip_code_review=True,
        privacy=True,
    )

    assert over == "complete"
    assert answers == []
    assert driver.prompts[1].startswith("# Finalize Phase")


@pytest.mark.asyncio
async def test_open_tasks_are_told_once_when_nothing_is_done_about_them(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    async def plans(session: FakeSession, loop: Path, prompt: str) -> None:
        await session.tool("TaskCreate", {"subject": "[mainline] write feature.py"})

    driver = FakeAgentDriver(reply=Builder(repo, {0: plans}))

    over = await _rlcr(
        repo, driver, _reviewer(DONE), skip_code_review=True, privacy=True
    )

    assert over == "complete"
    held = driver.prompts
    assert held[1].startswith("# Incomplete Tasks Detected")
    assert held[2].startswith("# Finalize Phase (Review Skipped)")


@pytest.mark.asyncio
async def test_a_killed_loop_is_picked_up_where_it_stood(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(looping, "_PAUSE", 0)
    repo = _repo(tmp_path)
    journal = tmp_path / "epic" / "journal.jsonl"
    # A base branch the loop never reviews against, which a resume must not trip on.
    params = {"skip_code_review": True, "privacy": True, "base_branch": "release"}

    async def killed(session: FakeSession, loop: Path, prompt: str) -> str:
        raise HarnessKilled("the builder died")

    first = FakeAgentDriver(reply=Builder(repo, {1: killed, 2: killed, 3: killed}))
    with pytest.raises(HarnessKilled):
        await run_fake(
            humanize1.rlcr,
            "build the plan",
            agents={"builder": first, "reviewer": _reviewer(NOT_DONE)},
            params=params,
            local=local_env(repo),
            journal=journal,
        )
    (loop,) = _loops(repo)
    assert _round(loop) == 1
    # Round 0, then round 1 taken three times before the loop gave up on the builder.
    assert len(first.prompts) == 4

    second = FakeAgentDriver(reply=Builder(repo))
    reviewer = _reviewer(DONE)
    over = await run_fake(
        humanize1.rlcr,
        "build the plan",
        agents={"builder": second, "reviewer": reviewer},
        params=params,
        local=local_env(repo),
        journal=journal,
        resume=True,
    )

    assert over == "complete"
    assert _loops(repo) == [loop]
    assert second.prompts[0] == (loop / "round-1-prompt.md").read_text()
    assert [p.splitlines()[0] for p in reviewer.prompts] == ["# Code Review - Round 1"]


@pytest.mark.asyncio
async def test_a_review_that_runs_too_long_is_sent_back_as_failed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    reviews = iter([None, DONE])

    async def slow(prompt: str, *, output_schema: Any, **_: Any) -> Any:
        if output_schema is not None:
            return {"relevant": True, "switches_branch": False, "why": "a feature"}
        said = next(reviews)
        if said is None:
            await asyncio.sleep(5)
        return said

    driver = FakeAgentDriver(reply=Builder(repo))
    over = await _rlcr(
        repo,
        driver,
        FakeAgentDriver(reply=slow),
        codex_timeout=1,
        skip_code_review=True,
        privacy=True,
    )

    assert over == "complete"
    failed = driver.prompts[1]
    assert failed.startswith("# Review Failed")
    assert "took longer than the 1s it was given" in failed


@pytest.mark.asyncio
async def test_the_quiz_is_put_to_the_person_who_can_stop_the_setup(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    person = FakeOutworlder([{"choice": "A"}, {"choice": "C"}, {"choice": "B"}])

    with pytest.raises(ValueError, match="review the plan file"):
        await _rlcr(repo, Builder(repo), _reviewer(DONE), outworlder=person)

    first, second, going = person.asked
    assert first.startswith("What does the plan add?")
    assert "\nA. a feature module\nB. a database" in first
    assert second.startswith("Which file says the round?")
    assert "The answers were Q1: A, Q2: B" in going
    assert not (repo / ".humanize" / "rlcr").exists()


@pytest.mark.asyncio
async def test_nobody_there_means_no_quiz(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    reviewer = _reviewer(DONE)
    away = FakeOutworlder(away=True)

    over = await _rlcr(
        repo,
        Builder(repo),
        reviewer,
        skip_code_review=True,
        privacy=True,
        outworlder=away,
    )

    assert over == "complete"
    assert away.asked == []
    assert not any("analyzes an implementation plan" in p for p in reviewer.prompts)


@pytest.mark.asyncio
async def test_rlcr_needs_a_git_repository(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plan.md").write_text(PLAN)

    with pytest.raises(ValueError, match="git repository"):
        await _rlcr(tmp_path, Builder(tmp_path), _reviewer(DONE))


def test_yolo_is_skip_quiz_and_claude_answer_codex() -> None:
    config = humanize1.Rlcr(yolo=True)

    assert config.skip_quiz
    assert config.claude_answer_codex
    assert json.loads(config.model_dump_json())["yolo"] is True


@pytest.mark.asyncio
async def test_a_builder_turn_that_breaks_once_is_taken_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(looping, "_PAUSE", 0)
    repo = _repo(tmp_path)

    async def dropped(session: FakeSession, loop: Path, prompt: str) -> str:
        raise HarnessDropped("the connection broke")

    driver = FakeAgentDriver(reply=Builder(repo, {0: dropped}))
    over = await _rlcr(
        repo, driver, _reviewer(DONE), skip_code_review=True, privacy=True
    )

    assert over == "complete"
    first, again, *_ = driver.prompts
    assert again == first


@pytest.mark.asyncio
async def test_a_reviewer_that_never_answers_ends_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(looping, "_PAUSE", 0)
    repo = _repo(tmp_path)
    attempts = 0

    async def broken(prompt: str, *, output_schema: Any, **_: Any) -> Any:
        nonlocal attempts
        if output_schema is not None:
            return {"relevant": True, "switches_branch": False, "why": "a feature"}
        attempts += 1
        raise HarnessMissing("the reviewer would not start")

    driver = FakeAgentDriver(reply=Builder(repo))
    with pytest.raises(HarnessMissing):
        await _rlcr(
            repo,
            driver,
            FakeAgentDriver(reply=broken),
            skip_code_review=True,
            privacy=True,
        )

    assert attempts == 9
    assert [p.splitlines()[0] for p in driver.prompts[1:]] == [
        "# Review Failed",
        "# Review Failed",
    ]
