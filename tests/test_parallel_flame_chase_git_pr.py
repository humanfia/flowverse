from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).parents[1]
FLOW = ROOT / "flows" / "parallel_flame_chase_git_pr"
PACKAGE = FLOW / "_parallel_flame_chase_git_pr"
sys.path[:0] = [str(FLOW)]

import pytest  # noqa: E402
from _parallel_flame_chase_git_pr.models import (  # noqa: E402
    InitialPlan,
    LaneBrief,
    LaneReport,
    MissionSpec,
)
from _parallel_flame_chase_git_pr.repository import (  # noqa: E402
    EXECUTABLES,
    JSONL_LINE_LIMIT,
    TOOLS,
    GitRunPaths,
    append_jsonl,
    commit_parents,
    create_fast_path_merge,
    deliveries,
    initialize_shadow_repository,
    main_sha,
    pr_trailer,
    publish_main,
)
from _parallel_flame_chase_git_pr.runtime import SKILL  # noqa: E402
from _parallel_flame_chase_git_pr.storage import CoordinationStore  # noqa: E402
from hmz.flows import (  # noqa: E402
    Budget,
    Flow,
    OutputTokensExceeded,
    ParamsError,
    load,
)
from hmz.runtime.flowing.environments import local_env  # noqa: E402
from hmz.runtime.flowing.fakes import (  # noqa: E402
    FakeAgentDriver,
    FakeOutworlder,
    FakeSession,
    run_fake,
)

if TYPE_CHECKING:
    from collections.abc import Callable

EVALUATOR = [sys.executable, "evaluator.py"]
LANE_ROLES = tuple(
    f"lane_{number}_actor_{actor}" for number in (1, 2, 3) for actor in "ab"
)
PLAN = InitialPlan(
    lanes=[
        LaneBrief(
            lane=f"lane-{number}",  # type: ignore[arg-type]
            mission=MissionSpec(
                title=f"Approach {number}",
                objective=f"Test approach {number}",
                success_criteria=["Produce evidence"],
                approach_class=f"class-{number}",
                information_question=f"Does approach {number} work?",
            ),
        )
        for number in range(1, 4)
    ]
)
PROGRESS = LaneReport(
    status="progress",
    summary="Evidence-backed progress.",
    next_step="Continue the experiment.",
)
BLOCKED = LaneReport(status="blocked", summary="Nothing left to try in this lane.")


def command(
    *arguments: str, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if check:
        result.check_returncode()
    return result


def install_tools(paths: GitRunPaths) -> None:
    paths.bin.mkdir(parents=True)
    for name, source in TOOLS.items():
        shutil.copy2(PACKAGE / source, paths.bin / name)
    for name in EXECUTABLES:
        (paths.bin / name).chmod(0o755)


def make_source(tmp_path: Path, *, repository: bool = False) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "TASK.md").write_text(
        "You MUST only modify `candidate.py`.\n"
        f"You MUST run `{shlex.join(EVALUATOR)}`.\n",
        encoding="utf-8",
    )
    (source / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (source / "evaluator.py").write_text(
        "from candidate import VALUE\nprint(f'CYCLES: {VALUE}')\n", encoding="utf-8"
    )
    if repository:
        command("git", "init", "--quiet", "--initial-branch=main", cwd=source)
        command("git", "add", "--all", cwd=source)
        command(
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "--quiet",
            "-m",
            "task",
            cwd=source,
        )
    return source


def test_shadow_git_pr_freezes_ready_head_and_publishes_merge(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    run_paths = GitRunPaths(tmp_path / "run")
    install_tools(run_paths)
    baseline = initialize_shadow_repository(run_paths, source)
    store = CoordinationStore(run_paths.database, run_paths.events)
    store.initialize(
        run_id="run-1",
        git_pr_enabled=True,
        global_knowledge_enabled=True,
        allowed_paths=["candidate.py"],
        trusted_evaluator_command=EVALUATOR,
    )

    lane = run_paths.lane("lane-1")
    command("git", "switch", "-c", "lane-1/improve", cwd=lane)
    (lane / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
    command("git", "add", "candidate.py", cwd=lane)
    command("git", "commit", "-m", "improve candidate", cwd=lane)
    command("git", "push", "-u", "origin", "HEAD", cwd=lane)
    evaluated = command(
        str(run_paths.bin / "pfc"), "evaluate", "--", *EVALUATOR, cwd=lane
    )
    receipt_id = json.loads(evaluated.stdout.splitlines()[-1])["receipt_id"]
    opened = command(
        str(run_paths.bin / "pfc"),
        "pr",
        "open",
        "--draft",
        "--title",
        "Improve candidate",
        "--hypothesis",
        "VALUE=2 improves the official evaluator",
        cwd=lane,
    )
    pr_id = json.loads(opened.stdout)["id"]
    command(
        str(run_paths.bin / "pfc"),
        "pr",
        "ready",
        pr_id,
        "--receipt",
        receipt_id,
        cwd=lane,
    )
    frozen_head = store.pr(pr_id)["head_sha"]

    command("git", "commit", "--allow-empty", "-m", "late mutation", cwd=lane)
    blocked = command(
        "git",
        "push",
        "origin",
        "HEAD:lane-1/improve",
        cwd=lane,
        check=False,
    )
    assert blocked.returncode != 0
    assert "frozen" in blocked.stderr

    assert store.activate_pr(pr_id)["id"] == pr_id  # type: ignore[index]
    merge_sha = create_fast_path_merge(
        run_paths,
        pr_id=pr_id,
        prior_sha=baseline,
        head_sha=str(frozen_head),
    )
    assert main_sha(run_paths.central) == merge_sha

    assert publish_main(
        run_paths.central,
        source,
        prior_sha=baseline,
        merge_sha=merge_sha,
    ) == ["candidate.py"]
    publish_main(
        run_paths.central,
        source,
        prior_sha=baseline,
        merge_sha=merge_sha,
    )
    store.finalize_merge(
        pr_id=pr_id,
        prior_main_sha=baseline,
        merge_sha=merge_sha,
        comparison={"review_mode": "receipt_fast_path"},
    )
    assert (source / "candidate.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert store.pr(pr_id)["status"] == "merged"
    assert len(store.ledger()) == 1
    assert store.ledger()[0]["receipt_ids"] == [receipt_id]


def test_the_runtime_tool_refuses_a_receipt_without_a_score(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    run_paths = GitRunPaths(tmp_path / "run")
    install_tools(run_paths)
    initialize_shadow_repository(run_paths, source)
    store = CoordinationStore(run_paths.database, run_paths.events)
    store.initialize(
        run_id="run-1",
        git_pr_enabled=True,
        global_knowledge_enabled=False,
        allowed_paths=["candidate.py"],
        trusted_evaluator_command=EVALUATOR,
    )
    lane = run_paths.lane("lane-2")
    pfc = str(run_paths.bin / "pfc")
    command("git", "switch", "-c", "lane-2/fast", cwd=lane)
    (lane / "candidate.py").write_text("VALUE = 'fast'\n", encoding="utf-8")
    command("git", "commit", "-am", "claim speed without a count", cwd=lane)
    command("git", "push", "-u", "origin", "HEAD", cwd=lane)
    evaluated = command(pfc, "evaluate", "--", *EVALUATOR, cwd=lane)
    receipt_id = json.loads(evaluated.stdout.splitlines()[-1])["receipt_id"]
    opened = command(
        pfc, "pr", "open", "--draft", "--title", "t", "--hypothesis", "h", cwd=lane
    )
    pr_id = json.loads(opened.stdout)["id"]
    command(pfc, "pr", "ready", pr_id, "--receipt", receipt_id, cwd=lane)

    tool = str(run_paths.bin / "pfc-runtime")
    root = ("--root", str(run_paths.root))
    scored = command(tool, *root, "score", pr_id, cwd=tmp_path, check=False)
    assert scored.returncode == 3  # noqa: PLR2004 -- a refusal
    assert "no CYCLES value" in scored.stderr
    listed = command(tool, *root, "store", "prs", '{"status": "ready"}', cwd=tmp_path)
    assert [pr["id"] for pr in json.loads(listed.stdout)] == [pr_id]


def test_fact_dependencies_are_quarantined_and_revoked_transitively(
    tmp_path: Path,
) -> None:
    store = CoordinationStore(
        tmp_path / "coordination.sqlite", tmp_path / "events.jsonl"
    )
    store.initialize(
        run_id="run-1",
        git_pr_enabled=False,
        global_knowledge_enabled=True,
        allowed_paths=["**"],
    )
    first = store.add_fact(
        {
            "statement": "The evaluator maximizes accuracy.",
            "importance": "Prevents optimizing the wrong direction.",
            "proof": "The evaluator source compares larger values as better.",
            "scope": ["official evaluator"],
            "evidence": ["evaluator.py:20"],
            "dependencies": [],
            "contradicts": [],
        }
    )
    second = store.add_fact(
        {
            "statement": "Candidate A is comparable under that evaluator.",
            "importance": "Makes its score reusable.",
            "proof": "Candidate A used the same evaluator entry point.",
            "scope": ["candidate A"],
            "evidence": ["receipt R1"],
            "dependencies": [first["id"]],
            "contradicts": [],
        }
    )
    assert {item["id"] for item in store.search_knowledge("")} == {
        first["id"],
        second["id"],
    }
    contradiction = store.add_fact(
        {
            "statement": "The evaluator minimizes accuracy.",
            "importance": "This conflicts with the recorded direction.",
            "proof": "A branch-local interpretation read the comparator differently.",
            "scope": ["unmerged branch interpretation"],
            "evidence": ["report R2"],
            "dependencies": [],
            "contradicts": [first["id"]],
        }
    )
    assert store.fact(str(first["id"]), include_inactive=True)["status"] == "conflicted"
    assert store.fact(str(second["id"]), include_inactive=True)["status"] == "stale"
    assert contradiction["status"] == "conflicted"
    assert store.search_knowledge("") == []
    assert store.revoke_fact(str(first["id"])) == {first["id"], second["id"]}
    assert store.search_knowledge("") == []


def test_experiment_memory_classifies_scope_without_git(tmp_path: Path) -> None:
    store = CoordinationStore(
        tmp_path / "coordination.sqlite", tmp_path / "events.jsonl"
    )
    store.initialize(
        run_id="memory-run",
        git_pr_enabled=False,
        global_knowledge_enabled=False,
        experiment_memory_enabled=True,
        allowed_paths=["candidate.py"],
    )
    intent = {
        "family": "scheduler/priority",
        "target": "build_kernel.schedule",
        "base_ref": "workspace:one",
        "parameters": {"bonus": "0..8"},
    }
    assert store.check_experiments(**intent)["classification"] == "unseen"
    record = store.begin_experiment(
        lane="lane-2",
        hypothesis="A priority sweep may reduce tail stalls.",
        **intent,
    )
    assert store.check_experiments(**intent)["classification"] == "active"
    store.finish_experiment(
        str(record["id"]),
        lane="lane-2",
        outcome="exhausted",
        best_score=1000,
        coverage_count=9,
        evidence=["Rfixture"],
        limitations=["Only one base was measured."],
        reopen_if=["The scheduling DAG changes."],
        next_frontier="Try a different engine balance.",
    )
    checked = store.check_experiments(**intent)
    assert checked["classification"] == "covered"
    assert checked["records"][0]["coverage_count"] == 9
    assert (
        store.check_experiments(**{**intent, "base_ref": "workspace:two"})[
            "classification"
        ]
        == "stale"
    )


def test_standalone_memory_evaluation_uses_content_identity(tmp_path: Path) -> None:
    source = tmp_path / "lane"
    source.mkdir()
    (source / "candidate.py").write_text("VALUE = 7\n", encoding="utf-8")
    (source / "evaluator.py").write_text("print('CYCLES: 7')\n", encoding="utf-8")
    run_root = tmp_path / "run"
    shared = run_root / "shared"
    shared.mkdir(parents=True)
    store = CoordinationStore(
        shared / "coordination.sqlite", shared / "coordination-events.jsonl"
    )
    store.initialize(
        run_id="memory-run",
        git_pr_enabled=False,
        global_knowledge_enabled=False,
        experiment_memory_enabled=True,
        allowed_paths=["candidate.py"],
        trusted_evaluator_command=EVALUATOR,
    )
    bin_dir = run_root / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "pfc"
    shutil.copy2(PACKAGE / "agent_cli.py", cli)
    shutil.copy2(PACKAGE / "storage.py", bin_dir / "pfc_storage.py")
    cli.chmod(0o755)
    common = (str(cli), "--run-root", str(run_root), "--lane", "lane-1")
    evaluated = command(*common, "evaluate", "--", *EVALUATOR, cwd=source)
    receipt = json.loads(evaluated.stdout.splitlines()[-1])
    assert receipt["commit_sha"].startswith("workspace:")
    begun = command(
        *common,
        "experiment",
        "begin",
        "--family",
        "constants",
        "--target",
        "candidate.py",
        "--base",
        "auto",
        "--hypothesis",
        "Measure the constant.",
        "--parameters-json",
        '{"value":7}',
        cwd=source,
    )
    experiment_id = json.loads(begun.stdout)["id"]
    finished = command(
        *common,
        "experiment",
        "finish",
        experiment_id,
        "--outcome",
        "improved",
        "--evidence",
        receipt["receipt_id"],
        cwd=source,
    )
    assert json.loads(finished.stdout)["best_score"] == 7


def test_report_share_delivers_each_record_once_and_flags_broken_lines(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "lane-2.jsonl"
    reports.touch()
    append_jsonl(reports, {"summary": "one"})
    with reports.open("ab") as handle:
        handle.write(b"not json\n[1]\n")
    complete = reports.stat().st_size
    with reports.open("ab") as handle:
        handle.write(b'{"partial": ')
    found, end = deliveries(
        reports, 0, source="lane-2", health="report", batch=(12, 128 * 1024)
    )
    assert [item.get("report", item.get("health")) for item in found] == [
        {"summary": "one"},
        "invalid_report_json",
        "invalid_report_shape",
    ]
    assert found[0]["report_id"].startswith("lane-2:0:")  # type: ignore[union-attr]
    assert end == complete
    assert deliveries(
        reports, end, source="lane-2", health="report", batch=(12, 1)
    ) == ([], end)
    [again], _ = deliveries(
        reports, complete * 2, source="system", health="system_report", batch=(1, 1)
    )
    assert again["report"] == {"summary": "one"}

    linked = tmp_path / "linked.jsonl"
    linked.symlink_to(reports)
    with pytest.raises(ValueError, match="linked"):
        append_jsonl(linked, {"summary": "two"})
    with pytest.raises(ValueError, match="exceeds"):
        append_jsonl(reports, {"summary": "x" * JSONL_LINE_LIMIT})


def test_canonical_git_pr_lite_configuration_is_git_only() -> None:
    params = load(str(FLOW)).expected_params
    config = params()
    assert config.git_pr_enabled is True  # type: ignore[attr-defined]
    assert config.global_knowledge_enabled is False  # type: ignore[attr-defined]
    assert config.experiment_memory_enabled is False  # type: ignore[attr-defined]
    assert config.token_efficient_enabled is False  # type: ignore[attr-defined]
    assert config.main_update_monitor_enabled is False  # type: ignore[attr-defined]
    forbidden_overrides = {
        "git_pr_enabled": False,
        "global_knowledge_enabled": True,
        "experiment_memory_enabled": True,
        "token_efficient_enabled": True,
        "main_update_monitor_enabled": True,
    }
    flow: Any = load(str(FLOW))
    for field, value in forbidden_overrides.items():
        with pytest.raises(ValueError):
            params.model_validate({field: value})
        with pytest.raises(ParamsError):
            flow.params_of({field: str(value).lower()})


# ------------------------------------------------------------------ the flow, run


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "humanize-home"
    monkeypatch.setenv("HUMANIZE_HOME", str(home))
    return home


@pytest.fixture
def flow() -> Flow:
    return load(str(FLOW))


def agents(**replies: Any) -> dict[str, FakeAgentDriver]:
    chosen = {"orchestrator": FakeAgentDriver(reply=PLAN)}
    for role in LANE_ROLES:
        chosen[role] = FakeAgentDriver(reply=replies.get(role, BLOCKED))
    return chosen


def sessions(driver: FakeAgentDriver) -> list[FakeSession]:
    return driver.sessions


async def run_until(
    flow: Flow,
    source: Path,
    chosen: dict[str, FakeAgentDriver],
    until: Callable[[], bool],
    *,
    journal: Path,
    resume: bool = False,
    task: str = "Improve candidate.py.",
) -> None:
    """Runs the flow until `until` holds, then stops it as a person pressing Ctrl-C would."""
    workspace = local_env(source)
    running = asyncio.ensure_future(
        run_fake(
            flow,
            task,
            agents=chosen,
            params={"rest_seconds": 0.05},
            local=workspace,
            journal=journal,
            resume=resume,
        )
    )
    try:
        async with asyncio.timeout(120):
            while not until():
                if running.done():
                    running.result()
                    pytest.fail("the flow ended on its own")
                await asyncio.sleep(0.05)
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
        await workspace.close()


async def shell(*argv: str, cwd: str, check: bool = True) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if check:
        assert process.returncode == 0, err.decode()
    return out.decode()


def run_root(session: FakeSession) -> Path:
    return Path(str(session.placement.workdir)).parents[1]


def state_of(root: Path) -> dict[str, Any]:
    try:
        return json.loads((root / "shared" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def system_kinds(root: Path, lane: str) -> list[str]:
    path = root / "shared" / "system-reports" / f"{lane}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)["kind"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.mark.asyncio
async def test_a_receipted_pr_merges_into_main_and_is_published_to_the_source(
    tmp_path: Path, home: Path, flow: Flow
) -> None:
    source = make_source(tmp_path, repository=True)
    source_head = command("git", "rev-parse", "HEAD", cwd=source).stdout
    opened: dict[str, str] = {}

    async def improve(prompt: str, *, output_schema: Any, session: FakeSession) -> Any:
        clone = str(session.placement.workdir)
        pfc = str(run_root(session) / "shared" / "bin" / "pfc")
        await shell("git", "switch", "-c", "lane-1/improve", cwd=clone)
        Path(clone, "candidate.py").write_text("VALUE = 0\n", encoding="utf-8")
        await shell("git", "commit", "-am", "improve candidate", cwd=clone)
        await shell("git", "push", "-u", "origin", "HEAD", cwd=clone)
        evaluated = await shell(pfc, "evaluate", "--", *EVALUATOR, cwd=clone)
        receipt = json.loads(evaluated.splitlines()[-1])["receipt_id"]
        pr = json.loads(
            await shell(
                pfc,
                "pr",
                "open",
                "--draft",
                "--title",
                "Zero",
                "--hypothesis",
                "VALUE=0 takes no cycles",
                cwd=clone,
            )
        )["id"]
        await shell(pfc, "pr", "ready", pr, "--receipt", receipt, cwd=clone)
        opened.update(pr=pr, receipt=receipt)
        return PROGRESS

    async def fail_evaluation(
        prompt: str, *, output_schema: Any, session: FakeSession
    ) -> Any:
        pfc = str(run_root(session) / "shared" / "bin" / "pfc")
        await shell(
            pfc,
            "evaluate",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(3)",
            cwd=str(session.placement.workdir),
            check=False,
        )
        return BLOCKED

    chosen = agents(lane_1_actor_a=improve, lane_2_actor_a=fail_evaluation)

    def merged() -> bool:
        lane_b = sessions(chosen["lane_1_actor_b"])
        if not lane_b or not lane_b[0].prompts:
            return False
        root = run_root(lane_b[0])
        ledger = root / "shared" / "official-ledger.json"
        return (
            ledger.exists()
            and len(json.loads(ledger.read_text(encoding="utf-8"))["entries"]) == 1
            and "invalid_evaluation_receipt" in system_kinds(root, "lane-2")
        )

    await run_until(flow, source, chosen, merged, journal=tmp_path / "journal.jsonl")

    assert (source / "candidate.py").read_text(encoding="utf-8") == "VALUE = 0\n"
    assert command("git", "rev-parse", "HEAD", cwd=source).stdout == source_head
    assert command("git", "status", "--porcelain", cwd=source).stdout == (
        " M candidate.py\n"
    )
    root = run_root(sessions(chosen["lane_1_actor_a"])[0])
    paths = GitRunPaths(root)
    assert command("git", "rev-list", "--count", "HEAD", cwd=paths.planning).stdout == (
        "1\n"
    )
    store = CoordinationStore(paths.database, paths.events)
    assert store.pr(opened["pr"])["status"] == "merged"
    [entry] = store.ledger()
    assert entry["pr_id"] == opened["pr"]
    assert entry["receipt_ids"] == [opened["receipt"]]
    assert entry["comparison"]["score"] == 0  # type: ignore[index]
    assert entry["comparison"]["review_mode"] == "receipt_fast_path"  # type: ignore[index]
    head = main_sha(paths.central)
    assert head == entry["merge_sha"]
    assert len(commit_parents(paths.central, head)) == 2  # noqa: PLR2004
    assert pr_trailer(paths.central, head) == opened["pr"]

    state = state_of(root)
    assert state["status"] == "stopped"
    assert state["git_pr"]["observed_main_sha"] == head
    assert state["git_pr"]["pending_comparison"] is None
    assert state["lanes"]["lane-2"]["blocked"] is True
    assert system_kinds(root, "lane-1") == ["pr_merged", "receipt_fast_path_feedback"]
    assert "pr_merged" in system_kinds(root, "lane-2")
    assert system_kinds(root, "lane-3") == ["pr_merged"]

    handoff = sessions(chosen["lane_1_actor_b"])[0].prompts[0]
    assert "lane-1-actor-b, taking turn 2" in handoff
    assert "Same-lane partner handoff" in handoff
    assert "Evidence-backed progress." in handoff
    assert '"kind": "pr_merged"' in handoff
    assert [skill.name for skill in sessions(chosen["lane_1_actor_a"])[0].skills] == [
        SKILL
    ]
    assert Path(str(sessions(chosen["orchestrator"])[0].placement.workdir)) == (
        paths.planning
    )


@pytest.mark.asyncio
async def test_every_lane_gets_an_isolated_clone_and_a_resumed_run_keeps_its_state(
    tmp_path: Path, home: Path, flow: Flow
) -> None:
    source = make_source(tmp_path)
    journal = tmp_path / "journal.jsonl"
    chosen = agents(**dict.fromkeys(LANE_ROLES, PROGRESS))

    def every_lane_took_a_turn() -> bool:
        return all(
            sessions(chosen[f"lane_{number}_actor_a"]) for number in (1, 2, 3)
        ) and all(sessions(chosen[f"lane_{number}_actor_b"]) for number in (1, 2, 3))

    await run_until(flow, source, chosen, every_lane_took_a_turn, journal=journal)

    root = run_root(sessions(chosen["lane_1_actor_a"])[0])
    for number in (1, 2, 3):
        lane = f"lane-{number}"
        [first, *_] = sessions(chosen[f"lane_{number}_actor_a"])
        assert Path(str(first.placement.workdir)) == root / "private" / lane
        prompt = first.prompts[0]
        assert f"`{lane}/<experiment>`" in prompt
        assert "an equal PR-authoring research lane" in prompt
        assert "sole integration owner" not in prompt
        assert first.permission.user == "all"
    state = state_of(root)
    run_id = state["run_id"]
    assert state["status"] == "stopped"
    assert (root / "shared" / "repository.git").is_dir()
    baseline = main_sha(root / "shared" / "repository.git")
    assert state["git_pr"]["observed_main_sha"] == baseline

    resumed = agents(**dict.fromkeys(LANE_ROLES, PROGRESS))

    def resumed_lanes_took_turns() -> bool:
        return all(
            sessions(resumed[f"lane_{number}_actor_{actor}"])
            for number in (1, 2, 3)
            for actor in "ab"
        )

    await run_until(
        flow, source, resumed, resumed_lanes_took_turns, journal=journal, resume=True
    )
    state = state_of(root)
    assert state["run_id"] == run_id
    assert main_sha(root / "shared" / "repository.git") == baseline
    assert sessions(resumed["orchestrator"]) == []
    assert all(state["lanes"][lane]["turns"] >= 2 for lane in state["lanes"])  # noqa: PLR2004


@pytest.mark.asyncio
async def test_a_revised_task_file_is_replanned_in_a_copy_of_the_source(
    tmp_path: Path, home: Path, flow: Flow
) -> None:
    source = make_source(tmp_path)
    journal = tmp_path / "journal.jsonl"
    first = agents(**dict.fromkeys(LANE_ROLES, PROGRESS))
    await run_until(
        flow,
        source,
        first,
        lambda: bool(sessions(first["lane_1_actor_a"])),
        journal=journal,
        task="continue",
    )
    root = run_root(sessions(first["lane_1_actor_a"])[0])
    assert state_of(root)["objective"].startswith("You MUST only modify")

    task_file = source / "TASK.md"
    task_file.write_text(task_file.read_text(encoding="utf-8") + "Go faster.\n")
    second = agents(**dict.fromkeys(LANE_ROLES, PROGRESS))
    seen: dict[str, str] = {}

    def replan(prompt: str, *, output_schema: Any, session: FakeSession) -> Any:
        planning = Path(str(session.placement.workdir))
        seen["task"] = (planning / "TASK.md").read_text(encoding="utf-8")
        return PLAN

    second["orchestrator"] = FakeAgentDriver(reply=replan)
    await run_until(
        flow,
        source,
        second,
        lambda: bool(
            sessions(second["lane_1_actor_a"]) or sessions(second["lane_1_actor_b"])
        ),
        journal=journal,
        resume=True,
        task="continue",
    )
    state = state_of(root)
    assert state["objective"].endswith("Go faster.")
    assert [event["kind"] for event in state["events"]] == ["objective_replanned"]
    [planner] = sessions(second["orchestrator"])
    planning = Path(str(planner.placement.workdir))
    assert planning != root / "shared" / "planning-workspace"
    assert seen["task"].endswith("Go faster.\n")
    assert not planning.exists()
    assert "Go faster." in planner.prompts[0]
    assert (root / "objective.md").read_text(encoding="utf-8").endswith("Go faster.\n")


@pytest.mark.asyncio
async def test_a_spent_budget_records_every_turn_taken_and_stops_the_run(
    tmp_path: Path, home: Path, flow: Flow
) -> None:
    source = make_source(tmp_path)
    chosen = agents(**dict.fromkeys(LANE_ROLES, PROGRESS))
    workspace = local_env(source)
    try:
        with pytest.raises(OutputTokensExceeded):
            await run_fake(
                flow,
                "Improve candidate.py.",
                agents=chosen,
                params={"rest_seconds": 0.05},
                budget=Budget(output_tokens=8),
                local=workspace,
                journal=tmp_path / "journal.jsonl",
            )
    finally:
        await workspace.close()
    taken = [
        session
        for role in LANE_ROLES
        for session in sessions(chosen[role])
        if session.prompts
    ]
    assert len(taken) >= 7  # noqa: PLR2004 -- the budget less the planning turn
    state = state_of(run_root(taken[0]))
    assert state["status"] == "stopped"
    assert sum(lane["turns"] for lane in state["lanes"].values()) == len(taken)
    assert not list(home.glob("envs/*/clones/owner-*"))


@pytest.mark.asyncio
async def test_a_second_run_over_the_same_source_is_refused(
    tmp_path: Path, home: Path, flow: Flow
) -> None:
    source = make_source(tmp_path)
    chosen = agents(**dict.fromkeys(LANE_ROLES, PROGRESS))
    workspace = local_env(source)
    running = asyncio.ensure_future(
        run_fake(
            flow,
            "Improve candidate.py.",
            agents=chosen,
            params={"rest_seconds": 0.05},
            local=workspace,
        )
    )
    other = local_env(source)
    try:
        async with asyncio.timeout(60):
            while not sessions(chosen["lane_1_actor_a"]):
                assert not running.done()
                await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match="owns source workspace"):
            await run_fake(flow, "Improve candidate.py.", agents=agents(), local=other)
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
        await workspace.close()
        await other.close()
    assert not list(home.rglob("repository.git"))


@pytest.mark.asyncio
async def test_git_pr_large_workspace_confirmation_accounts_for_all_working_trees(
    tmp_path: Path,
    home: Path,
    flow: Flow,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(3):
        (source / f"file-{index}.txt").write_text("data", encoding="utf-8")
    person = FakeOutworlder("Stop")
    chosen = agents()
    workspace = local_env(source)
    try:
        assert (
            await run_fake(
                flow,
                "Improve the implementation.",
                agents=chosen,
                params={
                    "confirm_large_workspace_copies": True,
                    "workspace_file_warning_threshold": 2,
                },
                outworlder=person,
                local=workspace,
            )
            is None
        )
    finally:
        await workspace.close()

    output = capsys.readouterr().out
    assert "5 Git working trees" in output
    assert "startup cancelled" in output
    assert len(person.asked) == 1
    assert "Start anyway" in person.asked[0]
    assert sessions(chosen["orchestrator"]) == []
    assert not list(home.rglob("repository.git"))
    assert not list(home.glob("envs/*/scratch/*"))


@pytest.mark.asyncio
async def test_an_absent_person_cannot_confirm_a_large_workspace(
    tmp_path: Path, home: Path, flow: Flow, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "big.txt").write_text("x" * 64, encoding="utf-8")
    workspace = local_env(source)
    try:
        await run_fake(
            flow,
            "Improve the implementation.",
            agents=agents(),
            params={
                "confirm_large_workspace_copies": True,
                "workspace_copy_warning_threshold_bytes": 1,
            },
            local=workspace,
        )
    finally:
        await workspace.close()
    assert "No interactive confirmation is available" in capsys.readouterr().out
    assert not list(home.rglob("repository.git"))
