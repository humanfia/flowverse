from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from _parallel_flame_chase.core.models import (
    ArtifactRef,
    CandidateSubmission,
    CheckpointIdentity,
    Deliverable,
    InitialPlan,
    LaneCheckpoint,
    LaneReport,
)
from _parallel_flame_chase.lanes.prompts import lane_prompt, planning_prompt
from _parallel_flame_chase.lanes.runtime import run_lane_session
from _parallel_flame_chase.persistence.checkpoints import checkpoint_report
from _parallel_flame_chase.persistence.events import ReportBus
from _parallel_flame_chase.persistence.leaderboard import (
    empty_leaderboard,
    with_submission,
)
from _parallel_flame_chase.persistence.workspace import (
    RunPaths,
    SourceLock,
    artifacts_still_match,
    initialize_paths,
    validate_deliverable,
    validate_runtime_layout,
)


def test_prompts_name_the_base_skill_and_fresh_session_contract() -> None:
    plan = planning_prompt(
        objective="Improve the implementation.",
        workspace_map={},
    )
    assert "`parallel-flame-chase` skill" in plan
    assert "only coordinator turn" in plan

    lane = lane_prompt(
        objective="Improve the implementation.",
        lane="lane-1",
        actor_role="lane-1-actor-a",
        turn=1,
        workspace_map={},
        mission=None,
        initial_brief={},
        unread_reports=[],
        checkpoint_path="checkpoint.json",
        artifact_root="artifacts",
        identity={},
        integration_item=None,
        runtime_status={},
    )
    assert "`parallel-flame-chase` skill" in lane
    assert "fresh session" in lane


def test_agent_output_schemas_require_every_object_property() -> None:
    """Codex strict structured output rejects optional object properties."""

    def inspect(value: object, path: str) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                assert set(value.get("required", [])) == set(properties), path
            for key, child in value.items():
                inspect(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{path}/{index}")

    for output in (InitialPlan, LaneReport):
        inspect(output.model_json_schema(), output.__name__)


def candidate_report(
    lane: str, value: float, *, metric: str = "cycles"
) -> dict[str, object]:
    return {
        "version": 1,
        "report_id": f"report-{lane}-{metric}-{value}",
        "at": "2026-08-27T00:00:00.000Z",
        "run_id": "run-1",
        "lane": lane,
        "actor": "a",
        "turn": 1,
        "mission_id": None,
        "generation": 0,
        "submission": CandidateSubmission(
            title=f"{lane} candidate",
            metric=metric,
            value=value,
            direction="minimize",
            evaluator="local evaluator exit 0",
            evidence=["accepted by the task-provided evaluator"],
        ).model_dump(mode="json"),
        "artifacts": [
            {
                "path": "candidate.py",
                "description": "reconstructable candidate",
                "size": 10,
                "sha256": "a" * 64,
            }
        ],
    }


def test_all_three_lanes_submit_to_one_shared_best_board() -> None:
    board = empty_leaderboard("run-1")
    became_best: list[bool] = []
    for lane, value in (("lane-1", 1100), ("lane-2", 900), ("lane-3", 1000)):
        board, candidate, changed = with_submission(
            board, candidate_report(lane, value)
        )
        assert candidate["lane"] == lane
        assert candidate["artifacts"][0]["sha256"] == "a" * 64
        became_best.append(changed)

    assert became_best == [True, True, False]
    assert board["submission_count"] == 3
    assert board["best"]["lane"] == "lane-2"
    assert board["best"]["value"] == 900.0
    assert len(board["leaders"]) == 1

    alternate = candidate_report("lane-3", 0.99, metric="accuracy")
    alternate["submission"]["direction"] = "maximize"
    board, _, changed = with_submission(board, alternate)
    assert changed is False
    assert board["best"]["value"] == 900.0
    assert len(board["leaders"]) == 2


def test_candidate_submission_requires_a_reconstructable_deliverable() -> None:
    with pytest.raises(ValueError, match="reconstructable deliverable"):
        LaneReport(
            status="progress",
            summary="Measured a candidate without publishing its files.",
            submission=CandidateSubmission(
                title="candidate",
                metric="cycles",
                value=1000,
                direction="minimize",
                evaluator="local evaluator",
            ),
        )


def test_report_bus_redelivers_until_acknowledged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    paths = RunPaths(tmp_path / "run", source)
    initialize_paths(paths, make_snapshots=False)
    bus = ReportBus(paths)
    bus.publish("lane-2", {"status": "progress", "summary": "useful evidence"})
    cursors: dict[str, object] = {}
    first, acknowledgements = bus.unread("lane-1", cursors)
    second, _ = bus.unread("lane-1", cursors)
    assert first == second
    assert first[0]["source_lane"] == "lane-2"
    bus.acknowledge("lane-1", cursors, acknowledgements)
    assert bus.unread("lane-1", cursors)[0] == []


def test_artifact_package_is_explicit_hashed_and_immutable(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    candidate = root / "candidate.patch"
    candidate.write_text("patch", encoding="utf-8")
    deliverable = Deliverable(
        title="candidate",
        approach_class="algorithm",
        artifacts=[ArtifactRef(path="candidate.patch", description="portable patch")],
        integration_notes="Apply and test.",
    )
    recorded = validate_deliverable(root, deliverable)
    assert artifacts_still_match(root, recorded)
    candidate.write_text("changed", encoding="utf-8")
    assert not artifacts_still_match(root, recorded)
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    escaping = deliverable.model_copy(
        update={"artifacts": [ArtifactRef(path="escape", description="bad link")]}
    )
    with pytest.raises(ValueError, match="escapes|regular file"):
        validate_deliverable(root, escaping)


class LaneRepairSequence:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *, suppress: bool, schema: type[Any]) -> LaneReport:
        assert suppress is False
        assert schema is LaneReport
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            raise ValueError(
                "the turn did not answer as a LaneReport: progress carried a deliverable"
            )
        return LaneReport(
            status="deliverable_ready",
            summary="The same session corrected its report.",
            deliverable=Deliverable(
                title="candidate",
                approach_class="algorithm",
                artifacts=[
                    ArtifactRef(path="candidate.patch", description="portable patch")
                ],
                integration_notes="Apply and validate the candidate.",
            ),
        )


def test_lane_repairs_invalid_report_in_the_same_session() -> None:
    session = LaneRepairSequence()
    report = run_lane_session(session, "Do the assigned work.")  # type: ignore[arg-type]
    assert report is not None
    assert report.status == "deliverable_ready"
    assert len(session.prompts) == 2
    assert "rejected by the LaneReport protocol" in session.prompts[1]


def test_checkpoint_recovery_requires_exact_generation(tmp_path: Path) -> None:
    path = tmp_path / "lane-2.json"
    checkpoint = LaneCheckpoint(
        identity=CheckpointIdentity(
            run_id="run-1",
            lane="lane-2",
            mission_id=None,
            generation=4,
            phase="working",
            updated_at=dt.datetime(2026, 8, 23, tzinfo=dt.UTC),
        ),
        report=LaneReport(
            status="progress",
            summary="A partial controlled probe landed.",
            evidence=["checkpoint evidence"],
        ),
    )
    path.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    exact = {
        "version": 1,
        "run_id": "run-1",
        "lane": "lane-2",
        "mission_id": None,
        "generation": 4,
    }
    assert checkpoint_report(path, None, exact) == checkpoint.report
    assert checkpoint_report(path, None, {**exact, "generation": 5}) is None


def test_source_lock_rejects_a_second_integration_owner(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    lock_path = tmp_path / "source.lock"
    with (
        SourceLock(lock_path, source, "run-1"),
        pytest.raises(RuntimeError, match="another parallel Flame Chase"),
        SourceLock(lock_path, source, "run-2"),
    ):
        pass


def test_runtime_control_log_cannot_be_replaced_with_a_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    paths = RunPaths(tmp_path / "run", source)
    paths.root.mkdir()
    initialize_paths(paths, make_snapshots=True)
    validate_runtime_layout(paths)
    report = paths.reports / "lane-2.jsonl"
    report.unlink()
    report.symlink_to(tmp_path / "outside.jsonl")
    with pytest.raises(RuntimeError, match="replaced or linked"):
        validate_runtime_layout(paths)
