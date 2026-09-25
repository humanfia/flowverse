from __future__ import annotations

import asyncio
import hashlib
import json
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
    Deliverable,
    InitialPlan,
    LaneBrief,
    LaneReport,
    MissionSpec,
)
from _parallel_flame_chase.persistence.probe import REFUSED  # noqa: E402
from _parallel_flame_chase.runtime import ParallelRuntime  # noqa: E402
from hmz.flows import (  # noqa: E402
    Budget,
    CostExceeded,
    HarnessMissing,
    OutputSchemaError,
    load,
)
from hmz.runtime.flowing.fakes import (  # noqa: E402
    FakeAgentDriver,
    FakeEnvDriver,
    FakeOutworlder,
    run_fake,
)

ACTORS = (
    "coordinator",
    "lane_1_actor_a",
    "lane_1_actor_b",
    "lane_2_actor_a",
    "lane_2_actor_b",
    "lane_3_actor_a",
    "lane_3_actor_b",
)


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


def under(env: FakeEnvDriver, root: str) -> dict[str, bytes]:
    prefix = f"{root.rstrip('/')}/"
    return {
        path.removeprefix(prefix): data
        for path, data in env.machine.items()
        if path.startswith(prefix)
    }


async def probe(command: Any, env: FakeEnvDriver) -> tuple[int, str, str] | None:
    """Answers the flow's run probe as `persistence/probe.py` would, from the fake files."""
    if not isinstance(command, tuple):
        return None
    at = next(
        (index for index, part in enumerate(command) if part.endswith("probe.py")),
        None,
    )
    if at is None:
        return None
    mode, first, *rest = command[at + 1 :]
    said: object = None
    if mode == "stats":
        files = under(env, first)
        said = {
            "regular_files": len(files),
            "total_bytes": sum(len(data) for data in files.values()),
        }
    elif mode == "init":
        source, kind, _, *lanes = rest
        logs = [f"{first}/shared/reports/{lane}.jsonl" for lane in lanes]
        if kind == "resume" and not all(log in env.machine for log in logs):
            return REFUSED, "", "resumable run is incomplete\n"
        for log in logs:
            if log not in env.machine:
                await env.write(log, b"")
        if kind == "fresh":
            copies = [f"{first}/shared/planning-workspace"]
            copies += [f"{first}/private/{lane}" for lane in lanes if lane != "lane-1"]
            for copy in copies:
                for path, data in under(env, source).items():
                    await env.write(f"{copy}/{path}", data)
    elif mode == "snapshot":
        said = not under(env, rest[0])
        if said:
            for path, data in under(env, first).items():
                await env.write(f"{rest[0]}/{path}", data)
    elif mode == "commit":
        files = rest[1:]
        for staged, target in zip(files[::2], files[1::2], strict=True):
            await env.write(target, env.machine[staged])
    elif mode == "append":
        _, staged, log = rest
        await env.write(log, env.machine[log] + env.machine[staged])
    elif mode == "artifacts":
        described = []
        for raw in rest:
            data = env.machine.get(f"{first}/{raw}")
            if data is None:
                return REFUSED, "", f"deliverable artifact is missing: {raw}\n"
            digest = hashlib.sha256(data).hexdigest()
            described.append({"path": raw, "size": len(data), "sha256": digest})
        said = described
    elif mode == "checkpoint":
        data = env.machine.get(first)
        said = {
            "fingerprint": None if data is None else hashlib.sha256(data).hexdigest(),
            "text": None if data is None or rest == ["fingerprint"] else data.decode(),
        }
    return 0, json.dumps(said), ""


def workspace(files: dict[str, str] | None = None) -> FakeEnvDriver:
    return FakeEnvDriver(files or {}, workdir="/source", run=probe)


def progress(prompt: str, *, output_schema: Any, session: Any) -> Any:
    if output_schema is InitialPlan:
        return PLAN
    return LaneReport(
        status="progress",
        summary="The actor inspected its owned workspace.",
        evidence=[f"cwd={session.placement.workdir}"],
        next_step="Continue the assigned falsifiable mission.",
    )


def agents(reply: Any = progress, **overrides: Any) -> dict[str, Any]:
    chosen: dict[str, Any] = {name: FakeAgentDriver(reply=reply) for name in ACTORS}
    chosen.update(overrides)
    return chosen


def lane_of(prompt: str) -> str:
    return prompt.split(" for ", 1)[1].split(" ", 1)[0]


async def rest(_: float) -> None:
    """Rests until every lane turn under way has landed, so that each pass sees them all."""
    turns = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("parallel-flame-")
    ]
    if turns:
        await asyncio.wait(turns, timeout=5)
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ParallelRuntime, "default_sleep", staticmethod(rest))
    monkeypatch.setattr(ParallelRuntime, "default_max_turns", 3)


async def run(task: str, local: FakeEnvDriver, **said: Any) -> Any:
    said.setdefault("agents", agents())
    return await run_fake(load(str(FLOW)), task, local=local, **said)


def drive(task: str, local: FakeEnvDriver, **said: Any) -> Any:
    return asyncio.run(run(task, local, **said))


def mirrors(local: FakeEnvDriver) -> dict[str, dict[str, Any]]:
    return {
        path.removesuffix("/shared/state.json"): json.loads(data)
        for path, data in local.machine.items()
        if path.endswith("/shared/state.json")
    }


def control(local: FakeEnvDriver) -> tuple[str, dict[str, Any]]:
    (found,) = mirrors(local).items()
    return found


def workdirs(driver: FakeAgentDriver) -> list[str]:
    return [str(session.placement.workdir) for session in driver.sessions]


def large_params(*, confirm: bool, **overrides: Any) -> dict[str, Any]:
    return {
        "confirm_large_workspace_copies": confirm,
        "workspace_file_warning_threshold": 2,
        **overrides,
    }


def three_files() -> FakeEnvDriver:
    return workspace({f"file-{index}.txt": "data" for index in range(3)})


def test_large_workspace_warns_without_asking_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local = three_files()
    person = FakeOutworlder("Stop")
    drive(
        "Improve the implementation.",
        local,
        params=large_params(confirm=False),
        outworlder=person,
        journal=tmp_path / "journal.jsonl",
    )

    output = capsys.readouterr().out
    assert "WARNING" in output
    assert "3 regular files" in output
    assert "3 workspace snapshots" in output
    assert "confirmation is disabled" in output
    assert not person.asked
    root, _ = control(local)
    assert f"{root}/private/lane-2/file-0.txt" in local.machine


def test_large_workspace_confirmation_stops_before_copying(
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = three_files()
    person = FakeOutworlder("Stop")
    chosen = agents()
    assert (
        drive(
            "Improve the implementation.",
            local,
            agents=chosen,
            params=large_params(confirm=True),
            outworlder=person,
        )
        is None
    )

    (asked,) = person.asked
    assert "Start anyway" in asked
    assert "Stop" in asked
    assert "no new copies were created" in capsys.readouterr().out
    assert local.scratches == []
    assert set(local.machine) == {f"/source/file-{index}.txt" for index in range(3)}
    assert not any(driver.sessions for driver in chosen.values())


def test_large_workspace_confirmation_allows_copying(tmp_path: Path) -> None:
    local = three_files()
    person = FakeOutworlder("A. Start anyway")
    drive(
        "Improve the implementation.",
        local,
        params=large_params(confirm=True),
        outworlder=person,
        journal=tmp_path / "journal.jsonl",
    )
    assert person.asked
    root, _ = control(local)
    assert f"{root}/shared/planning-workspace/file-0.txt" in local.machine
    assert f"{root}/private/lane-2/file-1.txt" in local.machine
    assert f"{root}/private/lane-3/file-2.txt" in local.machine


def test_large_workspace_confirmation_catches_large_bytes() -> None:
    local = workspace({"large.bin": "large"})
    person = FakeOutworlder("Stop")
    drive(
        "Improve the implementation.",
        local,
        params={
            "confirm_large_workspace_copies": True,
            "workspace_file_warning_threshold": 100,
            "workspace_copy_warning_threshold_bytes": 14,
        },
        outworlder=person,
    )
    assert person.asked
    assert local.scratches == []


def test_large_workspace_without_interactive_person_stops_when_confirmation_enabled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = three_files()
    drive(
        "Improve the implementation.",
        local,
        params=large_params(confirm=True),
        outworlder=FakeOutworlder("A. Start anyway", away=True),
    )
    assert "No interactive confirmation is available" in capsys.readouterr().out
    assert local.scratches == []


def test_initial_plan_failure_preserves_backend_diagnostics() -> None:
    def rejecting(prompt: str, *, output_schema: Any, session: Any) -> Any:
        assert prompt
        assert output_schema is InitialPlan
        raise OutputSchemaError("invalid_json_schema: missing required kind")

    coordinator = FakeAgentDriver(reply=rejecting)
    with pytest.raises(RuntimeError, match="invalid_json_schema") as caught:
        drive(
            "Improve the implementation.",
            workspace(),
            agents=agents(coordinator=coordinator),
        )
    assert "attempt 1" in str(caught.value)
    assert "after 3 fresh sessions" in str(caught.value)
    assert len(coordinator.sessions) == 3
    assert all(session.closed for session in coordinator.sessions)


def test_runtime_isolates_lanes_and_resumes_actor_turn(tmp_path: Path) -> None:
    local = workspace(
        {"TASK.md": "Improve the local implementation.\n", "owned.txt": "user work\n"}
    )

    async def private_work(prompt: str, *, output_schema: Any, session: Any) -> Any:
        if output_schema is not InitialPlan and lane_of(prompt) == "lane-2":
            await local.write(f"{session.placement.workdir}/probe.txt", b"lane-2\n")
        return progress(prompt, output_schema=output_schema, session=session)

    journal = tmp_path / "journal.jsonl"
    chosen = agents(private_work)
    drive("Improve the local implementation.", local, agents=chosen, journal=journal)

    root, state = control(local)
    run_id = state["run_id"]
    assert state["status"] == "test-complete"
    assert state["run_root"] == root
    assert root.endswith(f"parallel_flame_chase-{run_id}")
    assert all(state["lanes"][lane]["next_actor"] == 1 for lane in state["lanes"])
    assert local.text("owned.txt") == "user work\n"
    assert "probe.txt" not in local.files
    assert workdirs(chosen["coordinator"]) == [f"{root}/shared/planning-workspace"]
    assert workdirs(chosen["lane_1_actor_a"]) == ["/source"]
    assert workdirs(chosen["lane_2_actor_a"]) == [f"{root}/private/lane-2"]
    assert workdirs(chosen["lane_3_actor_a"]) == [f"{root}/private/lane-3"]
    assert local.machine[f"{root}/private/lane-2/owned.txt"] == b"user work\n"
    assert local.machine[f"{root}/private/lane-3/owned.txt"] == b"user work\n"
    assert all(
        session.closed for driver in chosen.values() for session in driver.sessions
    )
    first_prompt = chosen["lane_2_actor_a"].prompts[0]
    assert f"{root}/shared/artifacts/lane-2" in first_prompt
    assert f"{root}/shared/checkpoints/lane-2.json" in first_prompt
    assert local.clones == []
    assert local.scratches == sorted(
        [f"parallel_flame_chase-{run_id}", "parallel_flame_chase-lock"]
    )

    resumed = agents()
    drive("continue", local, agents=resumed, journal=journal, resume=True)
    _, state = control(local)
    assert state["run_id"] == run_id
    assert resumed["coordinator"].sessions == []
    assert resumed["lane_1_actor_b"].prompts
    assert resumed["lane_2_actor_b"].prompts
    assert resumed["lane_3_actor_b"].prompts
    assert not resumed["lane_1_actor_a"].prompts
    assert workdirs(resumed["lane_2_actor_b"]) == [f"{root}/private/lane-2"]
    assert local.machine[f"{root}/private/lane-2/probe.txt"] == b"lane-2\n"
    assert all(state["lanes"][lane]["turns"] == 2 for lane in state["lanes"])


def test_a_resumed_run_whose_files_are_gone_refuses_to_start(tmp_path: Path) -> None:
    local = workspace({"TASK.md": "Improve.\n"})
    journal = tmp_path / "journal.jsonl"
    drive("Improve.", local, journal=journal)
    _, state = control(local)
    asyncio.run(local.destroy_scratch(f"parallel_flame_chase-{state['run_id']}"))
    with pytest.raises(RuntimeError, match="resumable run is incomplete"):
        drive("continue", local, journal=journal, resume=True)
    assert not mirrors(local)


def test_a_fresh_run_is_not_resumed_without_resume(tmp_path: Path) -> None:
    local = workspace({"TASK.md": "Improve.\n"})
    drive("Improve.", local, journal=tmp_path / "one.jsonl")
    again = agents()
    drive("continue", local, agents=again, journal=tmp_path / "two.jsonl")
    assert len(mirrors(local)) == 2
    assert again["coordinator"].sessions


def test_lanes_take_turns_concurrently_in_their_own_workspaces() -> None:
    barrier = asyncio.Barrier(3)
    seen: dict[str, str] = {}

    async def meeting(prompt: str, *, output_schema: Any, session: Any) -> Any:
        if output_schema is InitialPlan:
            return PLAN
        seen[lane_of(prompt)] = str(session.placement.workdir)
        async with asyncio.timeout(5):
            await barrier.wait()
        return progress(prompt, output_schema=output_schema, session=session)

    drive("Improve.", workspace(), agents=agents(meeting))
    assert set(seen) == {"lane-1", "lane-2", "lane-3"}
    assert len(set(seen.values())) == 3
    assert seen["lane-1"] == "/source"


def candidate(local: FakeEnvDriver) -> Any:
    values = {"lane-1": 1100, "lane-2": 900, "lane-3": 1000}

    async def reply(prompt: str, *, output_schema: Any, session: Any) -> Any:
        if output_schema is InitialPlan:
            return PLAN
        lane = lane_of(prompt)
        root = prompt.split("Your artifact root is `", 1)[1].split("`", 1)[0]
        await local.write(f"{root}/candidate.py", f"# {lane} candidate\n".encode())
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

    return reply


def test_runtime_accepts_every_lane_submission_and_shares_the_best(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ParallelRuntime, "default_max_turns", 6)
    local = workspace()
    chosen = agents(candidate(local))
    drive(
        "Find the fastest correct local candidate.",
        local,
        agents=chosen,
        journal=tmp_path / "journal.jsonl",
    )

    root, state = control(local)
    board = state["candidate_board"]
    assert board["submission_count"] == 6
    assert board["best"]["lane"] == "lane-2"
    assert board["best"]["value"] == 900.0
    digest = hashlib.sha256(b"# lane-2 candidate\n").hexdigest()
    assert board["best"]["artifacts"][0]["sha256"] == digest
    assert all(
        state["latest_reports"][lane]["submission"] is not None
        for lane in ("lane-1", "lane-2", "lane-3")
    )
    leaderboard = f"{root}/shared/leaderboard.json"
    assert json.loads(local.machine[leaderboard]) == board
    for actor in ("lane_1_actor_b", "lane_2_actor_b", "lane_3_actor_b"):
        prompt = chosen[actor].prompts[0]
        assert leaderboard in prompt
        assert '"value": 900.0' in prompt
    lane_1_log = local.machine[f"{root}/shared/reports/lane-1.jsonl"].decode()
    assert lane_1_log.count("\n") == 2
    assert "lane-2 published" in chosen["lane_1_actor_b"].prompts[0]


def test_a_deliverable_whose_files_are_missing_is_a_failed_turn(
    tmp_path: Path,
) -> None:
    def unpublished(prompt: str, *, output_schema: Any, session: Any) -> Any:
        if output_schema is InitialPlan:
            return PLAN
        return LaneReport(
            status="deliverable_ready",
            summary="Claims a package it never wrote.",
            deliverable=Deliverable(
                title="ghost",
                approach_class="ghost",
                artifacts=[ArtifactRef(path="ghost.py", description="missing")],
                integration_notes="None.",
            ),
        )

    local = workspace()
    drive(
        "Improve.",
        local,
        agents=agents(progress, lane_3_actor_a=FakeAgentDriver(reply=unpublished)),
        journal=tmp_path / "journal.jsonl",
    )
    root, state = control(local)
    (failed,) = [event for event in state["events"] if event["kind"] == "turn_failed"]
    assert failed["lane"] == "lane-3"
    assert "deliverable artifact is missing: ghost.py" in failed["error"]
    log = local.machine[f"{root}/shared/reports/lane-3.jsonl"].decode().splitlines()
    assert json.loads(log[0])["status"] == "turn_failed"
    assert state["lanes"]["lane-3"]["blocked"] is False


def test_a_checkpoint_left_by_a_failed_turn_is_recovered(tmp_path: Path) -> None:
    async def checkpointing(prompt: str, *, output_schema: Any, session: Any) -> Any:
        if output_schema is InitialPlan:
            return PLAN
        if lane_of(prompt) != "lane-2":
            return progress(prompt, output_schema=output_schema, session=session)
        path = prompt.split("You may update `", 1)[1].split("`", 1)[0]
        identity = json.loads(
            prompt.split("and this exact identity:\n", 1)[1].split(
                "\nThe checkpoint", 1
            )[0]
        )
        checkpoint = {
            "version": 1,
            "identity": {
                **identity,
                "phase": "working",
                "updated_at": "2026-09-25T00:00:00Z",
            },
            "report": LaneReport(
                status="progress", summary="Checkpointed before the crash."
            ).model_dump(mode="json"),
        }
        await local.write(path, json.dumps(checkpoint).encode())
        raise HarnessMissing("the CLI died mid-turn")

    local = workspace()
    drive(
        "Improve.",
        local,
        agents=agents(checkpointing),
        journal=tmp_path / "journal.jsonl",
    )
    _, state = control(local)
    recovered = state["latest_reports"]["lane-2"]
    assert recovered["recovered_from_checkpoint"] is True
    assert recovered["summary"] == "Checkpointed before the crash."
    assert state["lanes"]["lane-2"]["turns"] == 1


class FailingStart(FakeAgentDriver):
    async def open(self, *_: Any, **__: Any) -> Any:
        raise HarnessMissing(f"{self.model} backend is temporarily unavailable")


def test_two_actor_startup_failures_block_only_the_affected_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ParallelRuntime, "default_max_turns", 4)
    local = workspace()
    drive(
        "Improve the implementation.",
        local,
        agents=agents(
            lane_2_actor_a=FailingStart(model="lane-2-a"),
            lane_2_actor_b=FailingStart(model="lane-2-b"),
        ),
        journal=tmp_path / "journal.jsonl",
    )
    root, state = control(local)
    assert state["status"] == "test-complete"
    assert state["lanes"]["lane-2"]["blocked"] is True
    assert state["lanes"]["lane-2"]["consecutive_failures"] == 2
    assert state["latest_reports"]["lane-2"]["status"] == "turn_failed"
    assert "temporarily unavailable" in state["latest_reports"]["lane-2"]["summary"]
    failures = local.machine[f"{root}/shared/reports/lane-2.jsonl"].decode()
    assert failures.count("\n") == 2
    assert state["lanes"]["lane-1"]["turns"] >= 2
    assert state["lanes"]["lane-3"]["turns"] >= 2


def test_changed_task_on_continue_replans_current_source_in_same_run(
    tmp_path: Path,
) -> None:
    local = workspace({"TASK.md": "First objective.\n"})
    journal = tmp_path / "journal.jsonl"
    drive("First objective.", local, journal=journal)
    root, state = control(local)
    run_id = state["run_id"]

    asyncio.run(local.write("new-source-evidence.txt", b"landed\n"))
    asyncio.run(local.write("TASK.md", b"Revised objective.\n"))
    resumed = agents()
    drive("continue", local, agents=resumed, journal=journal, resume=True)

    _, state = control(local)
    assert state["run_id"] == run_id
    assert state["objective"] == "Revised objective."
    (planning,) = workdirs(resumed["coordinator"])
    assert planning.startswith(f"{root}/shared/planning-revisions/")
    assert planning.endswith("-1")
    assert local.machine[f"{planning}/new-source-evidence.txt"] == b"landed\n"
    assert f"{root}/private/lane-2/new-source-evidence.txt" not in local.machine
    assert any(event["kind"] == "objective_replanned" for event in state["events"])


def test_source_lock_rejects_a_second_integration_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        local = workspace()
        started = asyncio.Event()
        release = asyncio.Event()

        async def holding(prompt: str, *, output_schema: Any, session: Any) -> Any:
            if output_schema is InitialPlan:
                return PLAN
            started.set()
            await release.wait()
            return progress(prompt, output_schema=output_schema, session=session)

        first = asyncio.create_task(
            run("Improve.", local, agents=agents(holding), journal=tmp_path / "1.jsonl")
        )
        async with asyncio.timeout(5):
            await started.wait()
        second = agents()
        with pytest.raises(RuntimeError, match="another parallel Flame Chase"):
            await run("Improve.", local, agents=second, journal=tmp_path / "2.jsonl")
        assert not any(driver.sessions for driver in second.values())
        release.set()
        async with asyncio.timeout(5):
            await first
        await run("Improve.", local, journal=tmp_path / "3.jsonl")

    asyncio.run(scenario())


def test_a_spent_budget_lets_turns_land_then_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ParallelRuntime, "default_max_turns", None)
    local = workspace()
    chosen = {name: FakeAgentDriver(reply=progress, cost=1.0) for name in ACTORS}
    with pytest.raises(CostExceeded):
        drive(
            "Improve.",
            local,
            agents=chosen,
            budget=Budget(cost=4.0),
            journal=tmp_path / "journal.jsonl",
        )
    _, state = control(local)
    assert state["status"] == "stopped"
    assert all(state["lanes"][lane]["turns"] == 1 for lane in state["lanes"])
    assert all(
        session.closed for driver in chosen.values() for session in driver.sessions
    )
