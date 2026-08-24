from __future__ import annotations

import json
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from _parallel_flame_chase.core.models import (
    InitialPlan,
    LaneBrief,
    LaneReport,
    MissionSpec,
)
from _parallel_flame_chase.orchestration import state as runtime_state
from hmz.flows import Stopped
from parallel_flame_chase_mission.coordination.models import AuditDecision
from parallel_flame_chase_mission.runtime.engine import MissionRuntime
from parallel_flame_chase_mission.runtime.engine import execute as execute_mission


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


def agents(agent_type: type[FakeAgent] = FakeAgent) -> SimpleNamespace:
    return SimpleNamespace(
        coordinator=agent_type("coordinator"),
        lane_1_actor_a=agent_type("lane-1-a"),
        lane_1_actor_b=agent_type("lane-1-b"),
        lane_2_actor_a=agent_type("lane-2-a"),
        lane_2_actor_b=agent_type("lane-2-b"),
        lane_3_actor_a=agent_type("lane-3-a"),
        lane_3_actor_b=agent_type("lane-3-b"),
    )


def mission_config(*, grace: float = 60.0) -> SimpleNamespace:
    return SimpleNamespace(
        resume_mode="auto",
        rest_seconds=0.001,
        global_audit_hours=6.0,
        mission_deadline_hours=6.0,
        max_turns_without_outcome=6,
        interrupt_grace_seconds=grace,
        external_events=None,
    )


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
    def new(self, cwd: str | Path | None = None) -> MissionSession:
        session = MissionSession(self, Path(cwd or ".").resolve())
        self.sessions.append(session)
        return session


def test_mission_runtime_audits_terminal_lane_without_stopping_other_lanes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "TASK.md").write_text("Improve the implementation.\n", encoding="utf-8")
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    chosen = agents(MissionAgent)
    state: dict[str, Any] = {}
    execute_mission(
        chosen,
        "Improve the implementation.",
        mission_config(),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=6,
    )
    completed = [
        audit for audit in state["missions"]["audits"] if audit["status"] == "completed"
    ]
    assert (Path(state["run_root"]) / "shared" / "audits").is_dir()
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
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    runtime = MissionRuntime(
        agents(MissionAgent),
        "Improve the implementation.",
        mission_config(grace=0.0),
        {},
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


def test_closed_stale_coordinator_is_not_mistaken_for_user_stop(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runtime_home = tmp_path / "humanize-home"
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: runtime_home)
    runtime = MissionRuntime(
        agents(MissionAgent),
        "Improve the implementation.",
        mission_config(),
        {},
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


def test_changed_task_on_continue_opens_immediate_global_replan(
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
    execute_mission(
        agents(),
        "First objective.",
        mission_config(),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    run_id = state["run_id"]
    task_file.write_text("Revised objective.\n", encoding="utf-8")
    runtime = MissionRuntime(
        agents(),
        "continue",
        mission_config(),
        state,
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
