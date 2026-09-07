from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from _parallel_flame_chase.core.api import BaseConfig, GitPRAgents
from _parallel_flame_chase.core.models import (
    InitialPlan,
    LaneBrief,
    LaneReport,
    MissionSpec,
)
from _parallel_flame_chase.orchestration import state as runtime_state
from hmz.flows import Stopped
from parallel_flame_chase_git_pr import Config
from parallel_flame_chase_git_pr.repository import (
    GitRunPaths,
    create_fast_path_merge,
    initialize_shadow_repository,
    main_sha,
    publish_main,
)
from parallel_flame_chase_git_pr.runtime import GitPRRuntime, execute
from parallel_flame_chase_git_pr.storage import CoordinationStore


class RuntimeConfig(BaseConfig):
    """Exercise the shared runtime's independently switchable internal mechanisms."""

    git_pr_enabled: bool = True
    global_knowledge_enabled: bool = False
    experiment_memory_enabled: bool = False
    token_efficient_enabled: bool = False
    main_update_monitor_enabled: bool = False


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


def test_shadow_git_pr_freezes_ready_head_and_publishes_merge(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "TASK.md").write_text(
        "You MUST only modify `candidate.py`.\nYou MUST run `python evaluator.py`.\n",
        encoding="utf-8",
    )
    (source / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (source / "evaluator.py").write_text(
        "from candidate import VALUE\nprint(f'CYCLES: {VALUE}')\n", encoding="utf-8"
    )
    run_paths = GitRunPaths(tmp_path / "run")
    run_paths.root.mkdir()
    implementation = Path(__file__).parents[1] / "flows" / "parallel_flame_chase_git_pr"
    baseline = initialize_shadow_repository(
        run_paths,
        source,
        cli_source=implementation / "agent_cli.py",
        storage_source=implementation / "storage.py",
        hook_source=implementation / "pre_receive.py",
    )
    store = CoordinationStore(run_paths.database, run_paths.events)
    store.initialize(
        run_id="run-1",
        git_pr_enabled=True,
        global_knowledge_enabled=True,
        allowed_paths=["candidate.py"],
        trusted_evaluator_command=["python", "evaluator.py"],
    )

    lane = run_paths.lane("lane-1")
    command("git", "switch", "-c", "lane-1/improve", cwd=lane)
    (lane / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
    command("git", "add", "candidate.py", cwd=lane)
    command("git", "commit", "-m", "improve candidate", cwd=lane)
    command("git", "push", "-u", "origin", "HEAD", cwd=lane)
    evaluated = command(
        str(run_paths.bin / "pfc"),
        "evaluate",
        "--",
        "python",
        "evaluator.py",
        cwd=lane,
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
        comparison={"trusted_orchestrateor": True},
    )
    assert (source / "candidate.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert store.pr(pr_id)["status"] == "merged"
    assert len(store.ledger()) == 1
    assert store.ledger()[0]["receipt_ids"] == [receipt_id]


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
        trusted_evaluator_command=["python", "evaluator.py"],
    )
    implementation = Path(__file__).parents[1] / "flows" / "parallel_flame_chase_git_pr"
    bin_dir = run_root / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "pfc"
    shutil.copy2(implementation / "agent_cli.py", cli)
    shutil.copy2(implementation / "storage.py", bin_dir / "pfc_storage.py")
    cli.chmod(0o755)
    common = (str(cli), "--run-root", str(run_root), "--lane", "lane-1")
    evaluated = command(*common, "evaluate", "--", "python", "evaluator.py", cwd=source)
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


class FakeSession:
    def __init__(self, agent: FakeAgent, cwd: Path) -> None:
        self.agent = agent
        self.cwd = cwd
        self.interjections: list[str] = []

    def __call__(self, prompt: str, *, suppress: bool, schema: type[Any]) -> Any:
        self.agent.prompts.append((self.cwd, prompt, schema))
        if schema is InitialPlan:
            return PLAN
        if schema is LaneReport:
            return LaneReport(
                status="progress",
                summary="Evidence-backed progress.",
                next_step="Continue the experiment.",
            )
        raise AssertionError(schema)

    def close(self) -> None:
        pass

    def interject(self, text: str) -> None:
        self.interjections.append(text)


class FakeAgent:
    def __init__(self) -> None:
        self.prompts: list[tuple[Path, str, type[Any]]] = []

    def new(self, cwd: str | Path | None = None) -> FakeSession:
        return FakeSession(self, Path(cwd or ".").resolve())


class FakeHuman:
    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.questions: list[Any] = []

    def asked(self, question: Any) -> str | None:
        self.questions.append(question)
        return self.answer


def agents(*, human_answer: str | None = None) -> GitPRAgents:
    return GitPRAgents(
        *(FakeAgent() for _ in range(7)),
        human=FakeHuman(human_answer),  # type: ignore[arg-type]
    )


def test_git_pr_agent_topology_has_no_reviewer_slot() -> None:
    assert GitPRAgents._fields == (
        "orchestrateor",
        "lane_1_actor_a",
        "lane_1_actor_b",
        "lane_2_actor_a",
        "lane_2_actor_b",
        "lane_3_actor_a",
        "lane_3_actor_b",
        "human",
    )


def test_canonical_git_pr_lite_configuration_is_git_only() -> None:
    config = Config()
    assert config.git_pr_enabled is True
    assert config.global_knowledge_enabled is False
    assert config.experiment_memory_enabled is False
    assert config.token_efficient_enabled is False
    assert config.main_update_monitor_enabled is False
    forbidden_overrides = {
        "git_pr_enabled": False,
        "global_knowledge_enabled": True,
        "experiment_memory_enabled": True,
        "token_efficient_enabled": True,
        "main_update_monitor_enabled": True,
    }
    for field, value in forbidden_overrides.items():
        with pytest.raises(ValueError):
            Config.model_validate({field: value})


def test_git_pr_large_workspace_confirmation_accounts_for_all_working_trees(
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
    runtime = GitPRRuntime(
        chosen,
        "Improve the implementation.",
        Config(
            confirm_large_workspace_copies=True,
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
    assert "5 Git working trees" in output
    assert chosen.human.questions  # type: ignore[attr-defined]
    assert not runtime_home.exists()


@pytest.mark.parametrize(
    ("git_enabled", "knowledge_enabled"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_every_factorial_cell_freezes_its_run_settings(
    tmp_path: Path,
    monkeypatch: Any,
    git_enabled: bool,
    knowledge_enabled: bool,
) -> None:
    source = tmp_path / f"source-{git_enabled}-{knowledge_enabled}"
    source.mkdir()
    (source / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: tmp_path / "humanize-home")
    state: dict[str, Any] = {}
    runtime = GitPRRuntime(
        agents(),
        "Improve candidate.py.",
        RuntimeConfig(
            rest_seconds=0.05,
            git_pr_enabled=git_enabled,
            global_knowledge_enabled=knowledge_enabled,
        ),
        state,
    )
    try:
        runtime.prepare()
        assert runtime.store.meta("git_pr_enabled") is git_enabled
        assert runtime.store.meta("global_knowledge_enabled") is knowledge_enabled
        assert runtime.git_paths.central.exists() is git_enabled
    finally:
        runtime._close_sessions()
        runtime.executor.shutdown(wait=True, cancel_futures=True)


def test_git_runtime_gives_every_lane_an_isolated_clone(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: tmp_path / "humanize-home")
    chosen = agents()
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve candidate.py.",
        RuntimeConfig(
            rest_seconds=0.05,
            git_pr_enabled=True,
            global_knowledge_enabled=False,
        ),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )

    root = Path(state["run_root"])
    assert state["git_pr_enabled"] is True
    assert (root / "shared" / "repository.git").is_dir()
    for lane, agent in zip(
        ("lane-1", "lane-2", "lane-3"),
        (
            chosen.lane_1_actor_a,
            chosen.lane_2_actor_a,
            chosen.lane_3_actor_a,
        ),
        strict=True,
    ):
        assert agent.prompts[0][0] == root / "private" / lane  # type: ignore[attr-defined]
        assert f"`{lane}/<experiment>`" in agent.prompts[0][1]  # type: ignore[attr-defined]
        assert "an equal PR-authoring research lane" in agent.prompts[0][1]  # type: ignore[attr-defined]
        assert "sole integration owner" not in agent.prompts[0][1]  # type: ignore[attr-defined]

    run_id = state["run_id"]
    baseline = main_sha(root / "shared" / "repository.git")
    resumed = agents()
    execute(
        resumed,
        "Improve candidate.py.",
        RuntimeConfig(
            rest_seconds=0.05,
            git_pr_enabled=False,
            global_knowledge_enabled=True,
        ),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    assert state["run_id"] == run_id
    assert state["git_pr_enabled"] is True
    assert state["global_knowledge_enabled"] is False
    assert main_sha(root / "shared" / "repository.git") == baseline
    assert resumed.orchestrateor.prompts == []  # type: ignore[attr-defined]


def test_experiment_memory_runtime_is_independent_of_git(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: tmp_path / "humanize-home")
    chosen = agents()
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve candidate.py.",
        RuntimeConfig(
            rest_seconds=0.05,
            git_pr_enabled=False,
            global_knowledge_enabled=False,
            experiment_memory_enabled=True,
        ),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )

    root = Path(state["run_root"])
    assert state["git_pr_enabled"] is False
    assert state["experiment_memory_enabled"] is True
    assert not (root / "shared" / "repository.git").exists()
    prompt = chosen.lane_1_actor_a.prompts[0][1]  # type: ignore[attr-defined]
    assert "Experiment Memory Lite" in prompt
    assert "independent of Git/PR" in prompt
    assert "experiment check" in prompt


def test_token_efficient_prompt_removes_redundant_model_checks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: tmp_path / "humanize-home")
    chosen = agents()
    state: dict[str, Any] = {}
    execute(
        chosen,
        "Improve candidate.py.",
        RuntimeConfig(
            rest_seconds=0.05,
            git_pr_enabled=True,
            global_knowledge_enabled=False,
            token_efficient_enabled=True,
        ),
        state,
        _sleep=lambda _: time.sleep(0.002),
        _max_turns=3,
    )
    prompt = chosen.lane_1_actor_a.prompts[0][1]  # type: ignore[attr-defined]
    assert "Token-efficient protocol" in prompt
    assert "do not rerun it when the relevant" in prompt


def test_main_update_monitor_interjects_active_lane_sessions(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(source)
    monkeypatch.setattr(runtime_state, "home", lambda: tmp_path / "humanize-home")
    runtime = GitPRRuntime(
        agents(),
        "Improve candidate.py.",
        RuntimeConfig(
            rest_seconds=0.05,
            git_pr_enabled=True,
            global_knowledge_enabled=False,
            main_update_monitor_enabled=True,
        ),
        {},
    )
    try:
        runtime.prepare()
        baseline = main_sha(runtime.git_paths.central)
        sessions: list[FakeSession] = []
        for lane_runtime in runtime.lanes.values():
            session = FakeSession(FakeAgent(), lane_runtime.workspace)
            lane_runtime.session = session  # type: ignore[assignment]
            lane_runtime.future = object()  # type: ignore[assignment]
            sessions.append(session)
        runtime._notify_main_update(
            prior=baseline,
            current=baseline,
            merged={"id": "PR000001", "lane": "lane-2"},
            changed=["candidate.py"],
            comparison={"prior_score": 100, "score": 90},
        )
        assert all(len(session.interjections) == 1 for session in sessions)
        assert all(
            "Do not answer this event separately" in session.interjections[0]
            for session in sessions
        )
        delivered = [
            event
            for event in runtime.store.telemetry()
            if event["kind"] == "main_update_monitor_delivered"
        ]
        assert len(delivered) == 3
        assert {
            event["payload"]["delivery"]  # type: ignore[index]
            for event in delivered
        } == {"interject"}
    finally:
        runtime._close_sessions()
        runtime.executor.shutdown(wait=True, cancel_futures=True)
