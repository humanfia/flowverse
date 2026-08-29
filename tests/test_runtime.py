from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _parallel_flame_chase.core.models import (
    ArtifactRef,
    CandidateSubmission,
    Deliverable,
    InitialPlan,
    LaneBrief,
    LaneReport,
    MissionSpec,
)
from _parallel_flame_chase.orchestration import state as runtime_state
from _parallel_flame_chase.runtime import ParallelRuntime, execute
from hmz.flows import Stopped


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


class CandidateSession(FakeSession):
    def __call__(self, prompt: str, *, suppress: bool, schema: type[Any]) -> Any:
        self.agent.prompts.append((self.cwd, prompt, schema))
        if schema is InitialPlan:
            return PLAN
        if schema is not LaneReport:
            raise AssertionError(schema)
        lane = self.agent.name.rsplit("-", 1)[0]
        values = {"lane-1": 1100, "lane-2": 900, "lane-3": 1000}
        marker = "Your artifact root is `"
        artifact_root = Path(prompt.split(marker, 1)[1].split("`", 1)[0])
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "candidate.py").write_text(
            f"# {lane} evaluator-accepted candidate\n", encoding="utf-8"
        )
        return LaneReport(
            status="deliverable_ready",
            summary=f"{lane} published an evaluator-accepted candidate.",
            evidence=["local evaluator exit 0"],
            deliverable=Deliverable(
                title=f"{lane} candidate",
                approach_class="test candidate",
                artifacts=[
                    ArtifactRef(path="candidate.py", description="complete candidate")
                ],
                integration_notes="Compare and reconstruct this candidate.",
            ),
            submission=CandidateSubmission(
                title=f"{lane} candidate",
                metric="cycles",
                value=values[lane],
                direction="minimize",
                evaluator="task-provided local evaluator, exit 0",
            ),
        )


class CandidateAgent(FakeAgent):
    def new(self, cwd: str | Path | None = None) -> CandidateSession:
        session = CandidateSession(self, Path(cwd or ".").resolve())
        self.sessions.append(session)
        return session


class FakeHuman:
    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.questions: list[Any] = []

    def asked(self, question: Any) -> str | None:
        self.questions.append(question)
        return self.answer


def agents(
    agent_type: type[FakeAgent] = FakeAgent,
    *,
    human_answer: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        coordinator=agent_type("coordinator"),
        lane_1_actor_a=agent_type("lane-1-a"),
        lane_1_actor_b=agent_type("lane-1-b"),
        lane_2_actor_a=agent_type("lane-2-a"),
        lane_2_actor_b=agent_type("lane-2-b"),
        lane_3_actor_a=agent_type("lane-3-a"),
        lane_3_actor_b=agent_type("lane-3-b"),
        human=FakeHuman(human_answer),
    )


def config() -> SimpleNamespace:
    return SimpleNamespace(
        resume_mode="auto",
        rest_seconds=0.001,
        workspace_file_warning_threshold=5_000,
    )


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


def test_large_workspace_warning_stops_before_snapshot_creation(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(3):
        (source / f"file-{index}.txt").write_text("data", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    chosen = agents(human_answer="Stop")
    runtime = ParallelRuntime(
        chosen,
        "Improve the implementation.",
        SimpleNamespace(
            resume_mode="auto",
            rest_seconds=0.001,
            workspace_file_warning_threshold=2,
        ),
        {},
    )
    try:
        with pytest.raises(Stopped, match="large workspace startup"):
            runtime.prepare()
    finally:
        runtime.executor.shutdown(wait=False, cancel_futures=True)

    output = capsys.readouterr().out
    assert "WARNING" in output
    assert "3 regular files" in output
    assert "three workspace snapshots" in output
    assert chosen.human.questions[0].options == ("Start anyway", "Stop")
    assert not runtime_home.exists()


def test_large_workspace_confirmation_allows_snapshot_creation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(3):
        (source / f"file-{index}.txt").write_text("data", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    chosen = agents(human_answer="A. Start anyway")
    runtime = ParallelRuntime(
        chosen,
        "Improve the implementation.",
        SimpleNamespace(
            resume_mode="auto",
            rest_seconds=0.001,
            workspace_file_warning_threshold=2,
        ),
        {},
    )
    try:
        runtime.prepare()
        root = runtime.paths.root
        assert root.is_dir()
        assert (root / "shared" / "planning-workspace").is_dir()
        assert (root / "private" / "lane-2").is_dir()
        assert (root / "private" / "lane-3").is_dir()
        assert (root / "shared" / "planning-workspace" / "file-0.txt").is_file()
        assert (root / "private" / "lane-2" / "file-1.txt").is_file()
        assert (root / "private" / "lane-3" / "file-2.txt").is_file()
        assert chosen.human.questions
    finally:
        runtime.executor.shutdown(wait=False, cancel_futures=True)


def test_large_workspace_without_interactive_person_stops_safely(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(3):
        (source / f"file-{index}.txt").write_text("data", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    runtime = ParallelRuntime(
        SimpleNamespace(coordinator=FakeAgent("coordinator")),
        "Improve the implementation.",
        SimpleNamespace(
            resume_mode="auto",
            rest_seconds=0.001,
            workspace_file_warning_threshold=2,
        ),
        {},
    )
    try:
        with pytest.raises(Stopped, match="requires confirmation"):
            runtime.prepare()
    finally:
        runtime.executor.shutdown(wait=False, cancel_futures=True)

    assert "No interactive confirmation is available" in capsys.readouterr().out
    assert not runtime_home.exists()


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

    # Simulate a compatible run created before the shared candidate board existed.
    state.pop("candidate_board")
    (root / "shared" / "leaderboard.json").unlink()
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
    assert state["candidate_board"]["submission_count"] == 0
    assert (root / "shared" / "leaderboard.json").is_file()
    assert resumed.coordinator.prompts == []
    assert resumed.lane_1_actor_b.prompts
    assert resumed.lane_2_actor_b.prompts
    assert resumed.lane_3_actor_b.prompts


def test_runtime_accepts_every_lane_submission_and_shares_the_best(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    chosen = agents(CandidateAgent)
    state: dict[str, Any] = {}

    execute(
        chosen,
        "Find the fastest correct local candidate.",
        config(),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=6,
    )

    board = state["candidate_board"]
    assert board["submission_count"] == 6
    assert board["best"]["lane"] == "lane-2"
    assert board["best"]["value"] == 900.0
    assert all(
        state["latest_reports"][lane]["submission"] is not None
        for lane in ("lane-1", "lane-2", "lane-3")
    )
    leaderboard = Path(state["run_root"]) / "shared/leaderboard.json"
    assert json.loads(leaderboard.read_text(encoding="utf-8")) == board
    for actor in (
        chosen.lane_1_actor_b,
        chosen.lane_2_actor_b,
        chosen.lane_3_actor_b,
    ):
        prompt = actor.prompts[0][1]
        assert str(leaderboard) in prompt
        assert '"value": 900.0' in prompt


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
