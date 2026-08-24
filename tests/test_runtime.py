from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _parallel_flame_chase.models import (
    AuditDecision,
    InitialPlan,
    LaneBrief,
    LaneReport,
    MissionSpec,
)
from _parallel_flame_chase.runtime import ParallelRuntime, execute
from _parallel_flame_chase.storage import (
    PROTECTED_FINGERPRINT_VERSION,
    tree_fingerprint,
)
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
        self.interjections: list[str] = []

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

    def interject(self, text: str) -> None:
        self.interjections.append(text)

    def close(self) -> None:
        self.closed = True


class FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.lane_calls = 0
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
    monkeypatch.setitem(execute.__globals__, "home", lambda: tmp_path / "humanize-home")
    coordinator = RejectingPlanAgent()
    runtime = ParallelRuntime(
        SimpleNamespace(coordinator=coordinator),
        "Improve the implementation.",
        config(),
        {},
        mission_mode=False,
    )
    try:
        with pytest.raises(RuntimeError, match="invalid_json_schema") as caught:
            runtime.prepare()
        assert "attempt 1" in str(caught.value)
        assert len(coordinator.sessions) == 3
        assert all(session.closed for session in coordinator.sessions)
    finally:
        runtime.executor.shutdown(wait=False, cancel_futures=True)


def config() -> SimpleNamespace:
    return SimpleNamespace(
        resume_mode="auto",
        protected_paths=(),
        rest_seconds=0.001,
    )


def mission_config(*, grace: float = 60.0) -> SimpleNamespace:
    return SimpleNamespace(
        resume_mode="auto",
        protected_paths=(),
        rest_seconds=0.001,
        global_audit_hours=6.0,
        mission_deadline_hours=6.0,
        max_turns_without_outcome=6,
        interrupt_grace_seconds=grace,
        external_events=None,
    )


def test_base_runtime_isolates_research_lanes_and_resumes_actor_turn(
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
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    chosen = agents()
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve the local implementation.",
        config(),
        state,
        mission_mode=False,
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
        mission_mode=False,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    assert state["run_id"] == run_id
    assert resumed.coordinator.prompts == []
    assert resumed.lane_1_actor_b.prompts
    assert resumed.lane_2_actor_b.prompts
    assert resumed.lane_3_actor_b.prompts


def test_resume_recovers_only_legacy_suppressed_report_block(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "TASK.md").write_text(
        "Improve the local implementation.\n", encoding="utf-8"
    )
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    state: dict[str, Any] = {}
    execute(
        agents(),
        "Improve the local implementation.",
        config(),
        state,
        mission_mode=False,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    lane = state["lanes"]["lane-1"]
    lane["blocked"] = True
    lane["consecutive_failures"] = 2
    lane["last_error"] = "actor returned no structured report"

    runtime = ParallelRuntime(
        agents(),
        "continue",
        config(),
        state,
        mission_mode=False,
        sleeper=lambda _: None,
    )
    runtime.prepare()
    recovered = runtime.control["lanes"]["lane-1"]
    assert recovered["blocked"] is False
    assert recovered["consecutive_failures"] == 0
    assert recovered["last_error"] is None
    assert any(
        event["kind"] == "legacy_protocol_block_recovered"
        and event["lane"] == "lane-1"
        and event["prior_consecutive_failures"] == 2
        for event in runtime.control["events"]
    )
    runtime.executor.shutdown(wait=False, cancel_futures=True)


class MissionSession(FakeSession):
    def __call__(self, prompt: str, *, suppress: bool, schema: type[Any]) -> Any:
        self.agent.prompts.append((self.cwd, prompt, schema))
        if schema is InitialPlan:
            return PLAN
        if schema is AuditDecision:
            raw = prompt.split("Evidence packet:\n", 1)[1].split(
                "\n\nReturn only the structured", 1
            )[0]
            packet = json.loads(raw)
            audit = packet["audit"]
            return AuditDecision(
                audit_id=audit["id"],
                revision=audit["revision"],
                lanes=[
                    {
                        "lane": lane,
                        "verdict": "redirect",
                        "reason": "The terminal evidence supports an orthogonal replacement.",
                        "replacement": spec("redirected mission", "architecture"),
                    }
                    for lane in audit["targets"]
                ],
            )
        if schema is LaneReport:
            self.agent.lane_calls += 1
            time.sleep(0.004)
            if self.agent.name == "lane-2-a" and self.agent.lane_calls == 1:
                return LaneReport(
                    status="no_result",
                    summary="The original lane-2 hypothesis was falsified.",
                    evidence=["negative controlled probe"],
                )
            return LaneReport(
                status="progress",
                summary=f"{self.agent.name} advanced its active mission.",
                evidence=[f"cwd={self.cwd}"],
                next_step="Continue the bounded mission.",
            )
        raise AssertionError(schema)


class MissionAgent(FakeAgent):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.lane_calls = 0

    def new(self, cwd: str | Path | None = None) -> MissionSession:
        session = MissionSession(self, Path(cwd or ".").resolve())
        self.sessions.append(session)
        return session


def mission_agents() -> SimpleNamespace:
    return SimpleNamespace(
        coordinator=MissionAgent("coordinator"),
        lane_1_actor_a=MissionAgent("lane-1-a"),
        lane_1_actor_b=MissionAgent("lane-1-b"),
        lane_2_actor_a=MissionAgent("lane-2-a"),
        lane_2_actor_b=MissionAgent("lane-2-b"),
        lane_3_actor_a=MissionAgent("lane-3-a"),
        lane_3_actor_b=MissionAgent("lane-3-b"),
    )


def test_mission_runtime_audits_terminal_lane_without_stopping_other_lanes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "TASK.md").write_text("Improve the implementation.\n", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    chosen = mission_agents()
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve the implementation.",
        mission_config(),
        state,
        mission_mode=True,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=6,
    )
    completed = [
        audit for audit in state["missions"]["audits"] if audit["status"] == "completed"
    ]
    assert completed
    assert completed[0]["scope"] == "targeted"
    assert completed[0]["targets"] == ["lane-2"]
    current_id = state["missions"]["current"]["lane-2"]
    current = next(
        item for item in state["missions"]["missions"] if item["id"] == current_id
    )
    assert current["spec"]["title"] == "redirected mission"
    assert state["lanes"]["lane-1"]["turns"] >= 2
    assert state["lanes"]["lane-3"]["turns"] >= 2
    assert len(chosen.coordinator.sessions) >= 2


class ProbeSession:
    def __init__(self) -> None:
        self.interjections: list[str] = []
        self.closed = False

    def interject(self, text: str) -> None:
        self.interjections.append(text)

    def close(self) -> None:
        self.closed = True


def test_targeted_audit_interjects_then_closes_only_its_target(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    runtime = ParallelRuntime(
        mission_agents(),
        "Improve the implementation.",
        mission_config(grace=0.0),
        {},
        mission_mode=True,
        sleeper=lambda _: None,
    )
    runtime.prepare()
    assert runtime.controller is not None
    runtime.controller.trigger(
        "test-review",
        {"event_id": "target-lane-2", "at": "2026-08-23T00:00:00Z"},
        scope="targeted",
        targets=["lane-2"],
    )
    target = runtime.lanes["lane-2"]
    probe = ProbeSession()
    target.session = probe  # type: ignore[assignment]
    target.future = Future()
    target.identity = runtime.controller.identity("lane-2")
    runtime._quiesce_targets()
    assert len(probe.interjections) == 1
    assert not probe.closed
    assert not runtime.controller.ready_for_decision()
    runtime._quiesce_targets()
    assert probe.closed
    assert runtime.controller.ready_for_decision()
    assert runtime.lanes["lane-1"].session is None
    assert runtime.lanes["lane-3"].session is None
    target.future.cancel()
    runtime.executor.shutdown(wait=False, cancel_futures=True)


class FailingStartAgent(FakeAgent):
    def new(self, cwd: str | Path | None = None) -> FakeSession:
        raise RuntimeError(f"{self.name} backend is temporarily unavailable")


def test_two_actor_startup_failures_block_only_the_affected_base_lane(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    chosen = agents()
    chosen.lane_2_actor_a = FailingStartAgent("lane-2-a")
    chosen.lane_2_actor_b = FailingStartAgent("lane-2-b")
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve the implementation.",
        config(),
        state,
        mission_mode=False,
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


def test_runtime_closed_stale_coordinator_is_not_mistaken_for_user_stop(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    runtime = ParallelRuntime(
        mission_agents(),
        "Improve the implementation.",
        mission_config(),
        {},
        mission_mode=True,
        sleeper=lambda _: None,
    )
    runtime.prepare()
    assert runtime.controller is not None
    runtime.controller.trigger(
        "first-review",
        {"event_id": "review-1", "at": "2026-08-23T00:00:00Z"},
        scope="targeted",
        targets=["lane-2"],
    )
    audit = runtime.controller.active_audit()
    assert audit is not None
    runtime.coordinator.audit_id = audit["id"]
    runtime.coordinator.revision = audit["revision"]
    runtime.coordinator.closed_for_stale_revision = True
    failed: Future[Any] = Future()
    failed.set_exception(Stopped())
    runtime.coordinator.future = failed
    runtime.controller.trigger(
        "newer-review",
        {"event_id": "review-2", "at": "2026-08-23T00:01:00Z"},
        scope="targeted",
        targets=["lane-3"],
    )
    runtime._collect_audit_decision()
    active = runtime.controller.active_audit()
    assert active is not None
    assert active["revision"] == 1
    assert runtime.coordinator.future is None
    assert runtime.coordinator.retry_after == 0.0
    assert runtime.coordinator.closed_for_stale_revision is False
    runtime.executor.shutdown(wait=False, cancel_futures=True)


def test_changed_task_on_base_continue_replans_current_source_in_same_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    task_file = source / "TASK.md"
    task_file.write_text("First objective.\n", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    state: dict[str, Any] = {}
    execute(
        agents(),
        "First objective.",
        config(),
        state,
        mission_mode=False,
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
        mission_mode=False,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    assert state["run_id"] == run_id
    planning_cwd = resumed.coordinator.prompts[0][0]
    assert planning_cwd.parent.name == "planning-revisions"
    assert (planning_cwd / "new-source-evidence.txt").is_file()
    assert any(event["kind"] == "objective_replanned" for event in state["events"])


def test_changed_task_on_mission_continue_opens_immediate_global_replan(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    task_file = source / "TASK.md"
    task_file.write_text("First objective.\n", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    state: dict[str, Any] = {}
    execute(
        agents(),
        "First objective.",
        mission_config(),
        state,
        mission_mode=True,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    run_id = state["run_id"]
    task_file.write_text("Revised objective.\n", encoding="utf-8")
    runtime = ParallelRuntime(
        agents(),
        "continue",
        mission_config(),
        state,
        mission_mode=True,
        sleeper=lambda _: None,
    )
    runtime.prepare()
    assert runtime.control["run_id"] == run_id
    assert runtime.controller is not None
    audit = runtime.controller.active_audit()
    assert audit is not None
    assert audit["scope"] == "global"
    assert audit["trigger_kind"] == "objective_revision"
    assert audit["targets"] == ["lane-1", "lane-2", "lane-3"]
    runtime.executor.shutdown(wait=False, cancel_futures=True)


class MutatingProtectedSession(FakeSession):
    def __call__(self, prompt: str, *, suppress: bool, schema: type[Any]) -> Any:
        if schema is LaneReport:
            (self.cwd / "protected.txt").write_text(
                "changed by lane\n", encoding="utf-8"
            )
        return super().__call__(prompt, suppress=suppress, schema=schema)


class MutatingProtectedAgent(FakeAgent):
    def new(self, cwd: str | Path | None = None) -> MutatingProtectedSession:
        session = MutatingProtectedSession(self, Path(cwd or ".").resolve())
        self.sessions.append(session)
        return session


def test_protected_path_change_blocks_lane_without_rolling_back_files(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "protected.txt").write_text("original\n", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    chosen = agents()
    chosen.lane_2_actor_a = MutatingProtectedAgent("lane-2-a")
    protected_config = config()
    protected_config.protected_paths = ("protected.txt",)
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve the implementation.",
        protected_config,
        state,
        mission_mode=False,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=4,
    )
    private_file = Path(state["run_root"]) / "private" / "lane-2" / "protected.txt"
    assert private_file.read_text(encoding="utf-8") == "changed by lane\n"
    assert (source / "protected.txt").read_text(encoding="utf-8") == "original\n"
    assert state["lanes"]["lane-2"]["blocked"] is True


def test_protected_fingerprint_ignores_only_real_generated_python_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    protected = source / "protected"
    protected.mkdir(parents=True)
    module = protected / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    before = tree_fingerprint(source, ("protected",))

    cache = protected / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-312.pyc").write_bytes(b"generated-one")
    assert tree_fingerprint(source, ("protected",)) == before
    (cache / "module.cpython-312.pyc").write_bytes(b"generated-two")
    assert tree_fingerprint(source, ("protected",)) == before

    module.write_text("VALUE = 2\n", encoding="utf-8")
    assert tree_fingerprint(source, ("protected",)) != before
    module.write_text("VALUE = 1\n", encoding="utf-8")
    (cache / "module.cpython-312.pyc").unlink()
    cache.rmdir()
    cache.symlink_to(module)
    assert tree_fingerprint(source, ("protected",)) != before


def test_resume_migrates_legacy_cache_fingerprint_and_unblocks_false_positive(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    protected = source / "protected"
    protected.mkdir(parents=True)
    (source / "TASK.md").write_text("Improve the implementation.\n", encoding="utf-8")
    (protected / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    cache = protected / "__pycache__"
    cache.mkdir()
    cached = cache / "module.cpython-312.pyc"
    cached.write_bytes(b"baseline-bytecode")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    protected_config = config()
    protected_config.protected_paths = ("protected",)
    state: dict[str, Any] = {}
    execute(
        agents(),
        "Improve the implementation.",
        protected_config,
        state,
        mission_mode=False,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )

    cache_entries = {
        "protected/__pycache__": {
            "kind": "directory",
            "mode": cache.stat().st_mode & 0o777,
        },
        "protected/__pycache__/module.cpython-312.pyc": {
            "kind": "file",
            "mode": cached.stat().st_mode & 0o777,
            "size": cached.stat().st_size,
            "sha256": hashlib.sha256(cached.read_bytes()).hexdigest(),
        },
    }
    for baseline in state["protected_baselines"].values():
        baseline["entries"].update(cache_entries)
        encoded = json.dumps(
            baseline["entries"], sort_keys=True, separators=(",", ":")
        ).encode()
        baseline["sha256"] = hashlib.sha256(encoded).hexdigest()
    state.pop("protected_fingerprint_version")
    blocked = state["lanes"]["lane-1"]
    blocked["blocked"] = True
    blocked["consecutive_failures"] = 2
    blocked["last_error"] = (
        "configured protected paths changed; the runtime blocked the lane without rollback"
    )
    for workspace in (
        source,
        Path(state["run_root"]) / "private" / "lane-2",
        Path(state["run_root"]) / "private" / "lane-3",
    ):
        (
            workspace / "protected" / "__pycache__" / "module.cpython-312.pyc"
        ).write_bytes(b"regenerated-bytecode")

    resumed = ParallelRuntime(
        agents(),
        "Improve the implementation.",
        protected_config,
        state,
        mission_mode=False,
        sleeper=lambda _: None,
    )
    try:
        resumed.prepare()
        assert resumed.control["protected_fingerprint_version"] == (
            PROTECTED_FINGERPRINT_VERSION
        )
        assert resumed.control["lanes"]["lane-1"]["blocked"] is False
        assert resumed.control["lanes"]["lane-1"]["consecutive_failures"] == 0
        assert resumed.control["lanes"]["lane-1"]["last_error"] is None
        assert any(
            event["kind"] == "generated_cache_false_positive_recovered"
            and event["lane"] == "lane-1"
            for event in resumed.control["events"]
        )
    finally:
        resumed.executor.shutdown(wait=False, cancel_futures=True)


def test_resume_rejects_report_log_modified_outside_authoritative_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setitem(execute.__globals__, "home", lambda: runtime_home)
    state: dict[str, Any] = {}
    execute(
        agents(),
        "Improve the implementation.",
        config(),
        state,
        mission_mode=False,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    report = Path(state["run_root"]) / "shared" / "reports" / "lane-2.jsonl"
    with report.open("a", encoding="utf-8") as handle:
        handle.write("{}\n")
    runtime = ParallelRuntime(
        agents(),
        "Improve the implementation.",
        config(),
        state,
        mission_mode=False,
        sleeper=lambda _: None,
    )
    with pytest.raises(RuntimeError, match="changed outside the runtime"):
        runtime.prepare()
    runtime.executor.shutdown(wait=False, cancel_futures=True)
