from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
FLOW = ROOT / "flows" / "parallel_flame_chase"
sys.path[:0] = [str(FLOW), str(FLOW.parent)]

import pytest  # noqa: E402
from _parallel_flame_chase.core.models import (  # noqa: E402
    ArtifactRef,
    CandidateSubmission,
    CheckpointIdentity,
    Deliverable,
    InitialPlan,
    LaneCheckpoint,
    LaneReport,
)
from _parallel_flame_chase.lanes.prompts import (  # noqa: E402
    lane_prompt,
    planning_prompt,
)
from _parallel_flame_chase.persistence.checkpoints import (  # noqa: E402
    checkpoint_report,
)
from _parallel_flame_chase.persistence.events import ReportBus  # noqa: E402
from _parallel_flame_chase.persistence.leaderboard import (  # noqa: E402
    empty_leaderboard,
    with_submission,
)
from _parallel_flame_chase.persistence.probe import (  # noqa: E402
    CHECKPOINT_FILE_LIMIT,
    REFUSED,
    RunPaths,
    WorkspaceStats,
    checkpoint_state,
    describe_artifacts,
    initialize_paths,
    inspect_workspace_stats,
    snapshot,
    validate_runtime_layout,
)
from _parallel_flame_chase.persistence.workspace import (  # noqa: E402
    PROBE,
    append_record,
    commit_files,
    initialize_run,
    inspect_workspace,
    validate_deliverable,
    validate_layout,
)
from hmz.flows import load  # noqa: E402
from hmz.runtime.flowing.fakes import (  # noqa: E402
    FakeAgentDriver,
    run_fake,
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


def test_workspace_inspection_counts_regular_files_and_apparent_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "one.txt").write_text("one", encoding="utf-8")
    (nested / "two.txt").write_text("two", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (source / "link.txt").symlink_to(outside)

    inspected = inspect_workspace_stats(source)

    assert inspected == WorkspaceStats(regular_files=2, total_bytes=6)


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


class DiskEnv:
    """An environment over a real directory, running commands as they are."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir

    async def read(self, path: str) -> bytes:
        return (self.workdir / path).read_bytes()

    async def write(self, path: str, data: bytes) -> None:
        target = self.workdir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def exec(self, argv: list[str], *, timeout: float) -> tuple[int, str, str]:
        done = await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=self.workdir,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout or None,
        )
        return done.returncode, done.stdout, done.stderr


def run_directory(tmp_path: Path) -> tuple[DiskEnv, RunPaths]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "work.txt").write_text("work", encoding="utf-8")
    paths = RunPaths(tmp_path / "run")
    paths.root.mkdir()
    initialize_paths(paths, source, list(LANES), make_snapshots=True)
    return DiskEnv(paths.root), paths


LANES = ("lane-1", "lane-2", "lane-3")


def test_report_bus_redelivers_until_acknowledged(tmp_path: Path) -> None:
    store, paths = run_directory(tmp_path)

    async def scenario() -> None:
        bus = ReportBus(store)
        await bus.open()
        assert paths.report_log("lane-2").read_bytes() == b""
        await bus.publish(
            "lane-2", {"status": "progress", "summary": "useful evidence"}
        )
        assert paths.report_log("lane-2").read_text().count("\n") == 1
        assert list(paths.staging.iterdir()) == []
        cursors: dict[str, Any] = {}
        first, acknowledgements = bus.unread("lane-1", cursors)
        second, _ = bus.unread("lane-1", cursors)
        assert first == second
        assert first[0]["source_lane"] == "lane-2"
        bus.acknowledge("lane-1", cursors, acknowledgements)
        assert bus.unread("lane-1", cursors)[0] == []

        reopened = ReportBus(store)
        await reopened.open()
        assert reopened.unread("lane-1", cursors)[0] == []
        assert reopened.unread("lane-3", {})[0][0]["report"]["summary"] == (
            "useful evidence"
        )

    asyncio.run(scenario())


def test_runtime_writes_never_go_through_a_planted_link(tmp_path: Path) -> None:
    store, paths = run_directory(tmp_path)
    victim = tmp_path / "source" / "work.txt"

    async def scenario() -> None:
        await commit_files(store, paths, LANES, {paths.leaderboard: b"{}\n"})
        assert paths.leaderboard.read_text() == "{}\n"
        paths.leaderboard.unlink()
        paths.leaderboard.symlink_to(victim)
        with pytest.raises(RuntimeError, match="replaced or linked"):
            await commit_files(store, paths, LANES, {paths.leaderboard: b"[]\n"})
        paths.leaderboard.unlink()
        log = paths.report_log("lane-2")
        log.unlink()
        log.symlink_to(victim)
        with pytest.raises(RuntimeError, match="replaced or linked"):
            await append_record(store, paths, LANES, log, b"{}\n")
        assert list(paths.staging.iterdir()) == []

    asyncio.run(scenario())
    assert victim.read_text() == "work"


def test_a_link_planted_after_the_check_is_replaced_not_followed(
    tmp_path: Path,
) -> None:
    _, paths = run_directory(tmp_path)
    victim = tmp_path / "source" / "work.txt"
    staged = paths.staging / "state.part"
    staged.write_text("{}", encoding="utf-8")
    paths.state_mirror.symlink_to(victim)
    staged.replace(paths.state_mirror)
    assert victim.read_text() == "work"
    assert not paths.state_mirror.is_symlink()


def test_artifact_package_is_explicit_hashed_and_immutable(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    candidate = root / "candidate.patch"
    candidate.write_text("patch", encoding="utf-8")
    recorded = describe_artifacts(root, ["candidate.patch"])
    assert recorded[0]["path"] == "candidate.patch"
    assert recorded[0]["size"] == 5
    candidate.write_text("changed", encoding="utf-8")
    assert describe_artifacts(root, ["candidate.patch"]) != recorded
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes|regular file"):
        describe_artifacts(root, ["escape"])
    (tmp_path / "linked").symlink_to(root)
    with pytest.raises(ValueError, match="replaced or linked"):
        describe_artifacts(tmp_path / "linked", ["candidate.patch"])


def test_runtime_control_log_cannot_be_replaced_with_a_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    lanes = ["lane-1", "lane-2", "lane-3"]
    paths = RunPaths(tmp_path / "run")
    paths.root.mkdir()
    initialize_paths(paths, source, lanes, make_snapshots=True)
    validate_runtime_layout(paths, lanes)
    staging = paths.staging
    staging.rmdir()
    staging.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="replaced or linked"):
        validate_runtime_layout(paths, lanes)
    staging.unlink()
    staging.mkdir()
    report = paths.report_log("lane-2")
    report.unlink()
    report.symlink_to(tmp_path / "outside.jsonl")
    with pytest.raises(ValueError, match="replaced or linked"):
        validate_runtime_layout(paths, lanes)


def test_a_resumed_run_directory_must_be_whole(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "work.txt").write_text("work", encoding="utf-8")
    lanes = ["lane-1", "lane-2", "lane-3"]
    paths = RunPaths(tmp_path / "run")
    paths.root.mkdir()
    initialize_paths(paths, source, lanes, make_snapshots=True)
    assert (paths.workspace("lane-2") / "work.txt").read_text() == "work"
    initialize_paths(paths, source, lanes, make_snapshots=False)
    for path in paths.workspace("lane-3").iterdir():
        path.unlink()
    paths.workspace("lane-3").rmdir()
    with pytest.raises(ValueError, match="refusing to recreate lost state"):
        initialize_paths(paths, source, lanes, make_snapshots=False)


def test_snapshot_is_whole_or_absent_and_never_inside_its_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "work.txt").write_text("first", encoding="utf-8")
    destination = tmp_path / "copies" / "one"
    assert snapshot(source, destination)
    (source / "work.txt").write_text("second", encoding="utf-8")
    assert not snapshot(source, destination)
    assert (destination / "work.txt").read_text() == "first"
    assert [path.name for path in destination.parent.iterdir()] == ["one"]
    with pytest.raises(ValueError, match="into itself"):
        snapshot(source, source / "run" / "copy")


def test_checkpoint_state_reads_only_small_regular_files(tmp_path: Path) -> None:
    regular = tmp_path / "lane-2.json"
    regular.write_text("{}", encoding="utf-8")
    said = checkpoint_state(regular)
    assert said["text"] == "{}"
    assert said["fingerprint"] is not None
    nothing = {"fingerprint": None, "text": None}
    linked = tmp_path / "linked.json"
    linked.symlink_to(regular)
    assert checkpoint_state(linked) == nothing
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    assert checkpoint_state(fifo) == nothing
    assert checkpoint_state(tmp_path / "missing.json") == nothing
    large = tmp_path / "large.json"
    large.write_bytes(b" " * (CHECKPOINT_FILE_LIMIT + 1))
    oversized = checkpoint_state(large)
    assert oversized["text"] is None
    assert str(oversized["fingerprint"]).endswith(":oversized")
    garbled = tmp_path / "garbled.json"
    garbled.write_bytes(b'{"summary": "\xff"}')
    undecodable = checkpoint_state(garbled)
    assert undecodable["fingerprint"] is not None
    assert undecodable["text"] is None
    assert checkpoint_state(regular, text=False)["text"] is None


def test_probe_script_answers_json_or_refuses(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text("x", encoding="utf-8")

    def probe(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(PROBE), *argv],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    stats = probe("stats", ".")
    assert stats.returncode == 0
    assert '"regular_files": 1' in stats.stdout
    refused = probe("artifacts", ".", "missing.py")
    assert refused.returncode == REFUSED
    assert "deliverable artifact is missing: missing.py" in refused.stderr


def test_probe_is_run_through_the_environment(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "work.txt").write_text("work", encoding="utf-8")
    workspace = DiskEnv(source)
    paths = RunPaths(tmp_path / "run")
    paths.root.mkdir()
    lanes = LANES
    deliverable = Deliverable(
        title="candidate",
        approach_class="algorithm",
        artifacts=[ArtifactRef(path="candidate.py", description="the candidate")],
        integration_notes="Apply and test.",
    )

    async def scenario() -> None:
        assert await inspect_workspace(workspace) == WorkspaceStats(1, 4)
        with pytest.raises(RuntimeError, match="refusing to recreate lost state"):
            await initialize_run(workspace, paths, lanes, fresh=False)
        await initialize_run(workspace, paths, lanes, fresh=True)
        await validate_layout(workspace, paths, lanes)
        assert (paths.workspace("lane-2") / "work.txt").read_text() == "work"
        root = paths.artifact_root("lane-2")
        (root / "candidate.py").write_text("candidate", encoding="utf-8")
        recorded = await validate_deliverable(workspace, root, deliverable)
        assert recorded[0]["description"] == "the candidate"
        assert recorded[0]["size"] == 9
        repeated = deliverable.model_copy(
            update={"artifacts": [*deliverable.artifacts, *deliverable.artifacts]}
        )
        with pytest.raises(ValueError, match="repeats artifact"):
            await validate_deliverable(workspace, root, repeated)
        missing = deliverable.model_copy(
            update={"artifacts": [ArtifactRef(path="gone.py", description="gone")]}
        )
        with pytest.raises(ValueError, match="missing: gone.py"):
            await validate_deliverable(workspace, root, missing)
        paths.leaderboard.symlink_to(source / "work.txt")
        with pytest.raises(RuntimeError, match="replaced or linked"):
            await validate_layout(workspace, paths, lanes)

    asyncio.run(scenario())


def test_lane_repairs_invalid_report_in_the_same_session() -> None:
    actor = FakeAgentDriver(
        reply=[
            {
                "status": "progress",
                "summary": "carried a deliverable",
                "deliverable": {},
            },
            LaneReport(
                status="deliverable_ready",
                summary="The same session corrected its report.",
                deliverable=Deliverable(
                    title="candidate",
                    approach_class="algorithm",
                    artifacts=[
                        ArtifactRef(
                            path="candidate.patch", description="portable patch"
                        )
                    ],
                    integration_notes="Apply and validate the candidate.",
                ),
            ),
        ]
    )
    report = asyncio.run(
        run_fake(
            load(f"{FLOW}:lane_turn"),
            "Do the assigned work.",
            agents={"actor": actor},
        )
    )
    assert report.status == "deliverable_ready"
    (session,) = actor.sessions
    assert session.closed
    assert len(session.prompts) == 2
    assert "rejected by the LaneReport protocol" in session.prompts[1]
    assert "caused by ValidationError" in session.prompts[1]


def test_lane_gives_up_after_two_repairs() -> None:
    actor = FakeAgentDriver(reply={"status": "invented"})
    with pytest.raises(ValueError, match="LaneReport"):
        asyncio.run(
            run_fake(load(f"{FLOW}:lane_turn"), "Work.", agents={"actor": actor})
        )
    (session,) = actor.sessions
    assert len(session.prompts) == 3


def test_checkpoint_recovery_requires_exact_generation() -> None:
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
    written = ("fingerprint", checkpoint.model_dump_json())
    exact = {
        "version": 1,
        "run_id": "run-1",
        "lane": "lane-2",
        "mission_id": None,
        "generation": 4,
    }
    assert checkpoint_report(written, None, exact) == checkpoint.report
    assert checkpoint_report(written, None, {**exact, "generation": 5}) is None
    assert checkpoint_report(written, "fingerprint", exact) is None
    assert checkpoint_report((None, None), None, exact) is None
    assert checkpoint_report(("other", "{"), None, exact) is None
