from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _parallel_flame_chase.core.models import (
    InitialPlan,
    LaneBrief,
    LaneReport,
    MissionSpec,
)
from _parallel_flame_chase.orchestration import state as runtime_state
from _parallel_flame_chase.runtime import ParallelRuntime, execute


def spec(title: str, approach: str) -> MissionSpec:
    return MissionSpec(
        title=title,
        objective=f"Investigate {title}",
        success_criteria=["Land one evidence-backed increment"],
        approach_class=approach,
        change_scale="component",
        information_question=f"What does {title} reveal?",
    )


PLAN = InitialPlan(
    lanes=[
        LaneBrief(lane="lane-1", mission=spec("integration", "baseline")),
        LaneBrief(lane="lane-2", mission=spec("algorithm", "algorithm")),
        LaneBrief(lane="lane-3", mission=spec("validation", "validation")),
    ]
)


class FakeSession:
    def __init__(self, agent: FakeAgent, cwd: Path) -> None:
        self.agent = agent
        self.cwd = cwd
        self.closed = False

    def __call__(self, prompt: str, *, suppress: bool, schema: type[Any]) -> Any:
        self.agent.prompts.append((self.cwd, prompt, schema))
        if schema is InitialPlan:
            return PLAN
        if schema is LaneReport:
            return LaneReport(
                status="progress",
                summary=f"{self.agent.name} inspected its owned workspace.",
                evidence=[f"cwd={self.cwd}"],
                next_step="Continue the assigned falsifiable mission.",
            )
        raise AssertionError(schema)

    def close(self) -> None:
        self.closed = True


class FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.prompts: list[tuple[Path, str, type[Any]]] = []
        self.sessions: list[FakeSession] = []

    def new(self, cwd: str | Path | None = None) -> FakeSession:
        session = FakeSession(self, Path(cwd or ".").resolve())
        self.sessions.append(session)
        return session


def agents() -> SimpleNamespace:
    return SimpleNamespace(
        coordinator=FakeAgent("coordinator"),
        lane_1_actor_a=FakeAgent("lane-1-a"),
        lane_1_actor_b=FakeAgent("lane-1-b"),
        lane_2_actor_a=FakeAgent("lane-2-a"),
        lane_2_actor_b=FakeAgent("lane-2-b"),
        lane_3_actor_a=FakeAgent("lane-3-a"),
        lane_3_actor_b=FakeAgent("lane-3-b"),
    )


def config() -> SimpleNamespace:
    return SimpleNamespace(resume_mode="auto", rest_seconds=0.001)


class RejectingPlanSession:
    def __init__(self) -> None:
        self.closed = False

    def __call__(self, prompt: str, *, suppress: bool, schema: type[Any]) -> Any:
        assert prompt
        assert suppress is False
        assert schema is InitialPlan
        raise RuntimeError("invalid_json_schema: missing required kind")

    def close(self) -> None:
        self.closed = True


class RejectingPlanAgent:
    def __init__(self) -> None:
        self.sessions: list[RejectingPlanSession] = []

    def new(self, cwd: str | Path | None = None) -> RejectingPlanSession:
        assert cwd is not None
        session = RejectingPlanSession()
        self.sessions.append(session)
        return session


def test_initial_plan_failure_preserves_backend_diagnostics(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: tmp_path / "humanize-home")
    coordinator = RejectingPlanAgent()
    runtime = ParallelRuntime(
        SimpleNamespace(coordinator=coordinator),
        "Improve the implementation.",
        config(),
        {},
    )
    try:
        with pytest.raises(RuntimeError, match="invalid_json_schema") as caught:
            runtime.prepare()
        assert "attempt 1" in str(caught.value)
        assert len(coordinator.sessions) == 3
        assert all(session.closed for session in coordinator.sessions)
    finally:
        runtime.executor.shutdown(wait=False, cancel_futures=True)


def test_runtime_isolates_lanes_and_resumes_actor_turn(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "TASK.md").write_text(
        "Improve the local implementation.\n", encoding="utf-8"
    )
    (source / "owned.txt").write_text("user work\n", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    chosen = agents()
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve the local implementation.",
        config(),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    run_id = state["run_id"]
    root = Path(state["run_root"])
    assert state["status"] == "test-complete"
    assert root.is_relative_to(runtime_home)
    assert (root / "private" / "lane-2" / "owned.txt").read_text() == "user work\n"
    assert (root / "private" / "lane-3" / "owned.txt").read_text() == "user work\n"
    assert (source / "owned.txt").read_text() == "user work\n"
    assert all(state["lanes"][lane]["next_actor"] == 1 for lane in state["lanes"])
    assert chosen.coordinator.prompts[0][0] == root / "shared" / "planning-workspace"

    resumed = agents()
    execute(
        resumed,
        "continue",
        config(),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    assert state["run_id"] == run_id
    assert resumed.coordinator.prompts == []
    assert resumed.lane_1_actor_b.prompts
    assert resumed.lane_2_actor_b.prompts
    assert resumed.lane_3_actor_b.prompts


class FailingStartAgent(FakeAgent):
    def new(self, cwd: str | Path | None = None) -> FakeSession:
        raise RuntimeError(f"{self.name} backend is temporarily unavailable")


def test_two_actor_startup_failures_block_only_the_affected_lane(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    chosen = agents()
    chosen.lane_2_actor_a = FailingStartAgent("lane-2-a")
    chosen.lane_2_actor_b = FailingStartAgent("lane-2-b")
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve the implementation.",
        config(),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=4,
    )
    assert state["status"] == "test-complete"
    assert state["lanes"]["lane-2"]["blocked"] is True
    assert state["lanes"]["lane-2"]["consecutive_failures"] == 2
    assert state["latest_reports"]["lane-2"]["status"] == "turn_failed"
    failures = (
        Path(state["run_root"]) / "shared" / "reports" / "lane-2.jsonl"
    ).read_text(encoding="utf-8")
    assert failures.count("\n") == 2
    assert state["lanes"]["lane-1"]["turns"] >= 2
    assert state["lanes"]["lane-3"]["turns"] >= 2


def test_changed_task_on_continue_replans_current_source_in_same_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    task_file = source / "TASK.md"
    task_file.write_text("First objective.\n", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    state: dict[str, Any] = {}
    execute(
        agents(),
        "First objective.",
        config(),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    run_id = state["run_id"]
    (source / "new-source-evidence.txt").write_text("landed\n", encoding="utf-8")
    task_file.write_text("Revised objective.\n", encoding="utf-8")
    resumed = agents()
    execute(
        resumed,
        "continue",
        config(),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    assert state["run_id"] == run_id
    planning_cwd = resumed.coordinator.prompts[0][0]
    assert planning_cwd.parent.name == "planning-revisions"
    assert (planning_cwd / "new-source-evidence.txt").is_file()
    assert any(event["kind"] == "objective_replanned" for event in state["events"])
