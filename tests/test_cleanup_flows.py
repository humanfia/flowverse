from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hmz.flows import (
    AgentCollection,
    Budget,
    CostExceeded,
    DurationExceeded,
    EnvCollection,
    FlowParams,
    Outworlder,
    ParamsError,
    flow,
)
from hmz.runtime.flowing.engine import load_flow
from hmz.runtime.flowing.fakes import FakeAgentDriver, FakeOutworlder, run_fake

from tests.cleanup_env import local

FLOWS = Path(__file__).parents[1] / "flows"
NAMES = ("flame_chase_agent_cleanup", "ralph_loop_agent_cleanup")
ROLES = {
    "flame_chase_agent_cleanup": ("first_chaser", "second_chaser", "cleaner"),
    "ralph_loop_agent_cleanup": ("agent", "cleaner"),
}
FIELDS = {
    "work_paths",
    "cleanup_turns",
    "next_lines",
    "comment_lines",
    "repairs",
    "check_command",
    "session_timeout_minutes",
    "idle_timeout_minutes",
    "stop_grace_minutes",
    "max_tracked_file_mb",
    "confirm_large_workspace_copies",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _load(name: str) -> Any:
    return load_flow(str(FLOWS / name), caller_globals={})


def _module(name: str, part: str) -> Any:
    return sys.modules[f"_{name}.{part}"]


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NAMES:
        _load(name)
        monkeypatch.setattr(_module(name, "guard"), "PAUSE", 0.0)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HUMANIZE_HOME", str(tmp_path / "humanize"))
    return tmp_path / "humanize"


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("value = 1\n")
    (root / "README.md").write_text("task\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "task")
    return root


def _speaker(
    name: str,
    events: list[str],
    answers: list[Any] | None = None,
    act: Callable[[], None] | None = None,
    cost: float = 1.0,
) -> FakeAgentDriver:
    queue = list(answers or [])

    def reply(_prompt: str, *, session: Any, output_schema: Any) -> Any:
        events.append(name)
        if act is not None:
            act()
        return queue.pop(0) if queue else "done"

    return FakeAgentDriver(reply=reply, cost=cost)


def _kept(journal: Path) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for line in journal.read_text().splitlines():
        record = json.loads(line)
        if record["t"] == "set":
            state[record["key"]] = record["value"]
        elif record["t"] == "del":
            state.pop(record["key"], None)
    return state


def _cleans(name: str, events: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    async def clean(cleaner: Any, *_args: Any) -> None:
        assert cleaner.role == "cleaner"
        events.append("clean")

    monkeypatch.setattr(_module(name, "loop"), "clean_epoch", clean)


@pytest.mark.parametrize("name", NAMES)
def test_flows_are_resumable_and_declare_their_roles_and_params(name: str) -> None:
    flow = _load(name)
    declared = flow.describe()

    assert declared.name == name
    assert flow.resumable
    assert flow.description
    assert tuple(role.name for role in declared.agents if not role.auto) == ROLES[name]
    (human,) = [role for role in declared.agents if role.auto]
    assert (human.name, human.declared) == ("human", Outworlder)
    (workspace,) = declared.envs
    assert (workspace.name, workspace.auto) == ("workspace", True)
    model = flow.expected_params
    assert issubclass(model, FlowParams)
    assert set(model.model_fields) == FIELDS
    held = model(work_paths=("src",))
    assert (held.cleanup_turns, held.next_lines, held.comment_lines) == (3, 10, 30)
    assert (held.repairs, held.check_command) == (2, "")
    assert held.session_timeout_minutes == 240
    assert held.idle_timeout_minutes == 20
    assert held.stop_grace_minutes == 10
    assert held.max_tracked_file_mb == 10
    assert held.confirm_large_workspace_copies is True


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.asyncio
async def test_a_run_nobody_set_up_names_work_paths(name: str, repo: Path) -> None:
    with pytest.raises(ParamsError, match="work_paths"):
        await run_fake(_load(name), "task", local=local(repo))


@pytest.mark.parametrize(
    "value", [(), ("../outside",), ("src", "src/generated"), ("src", "src"), (".git",)]
)
def test_work_paths_must_be_safe_and_non_overlapping(value: tuple[str, ...]) -> None:
    model = _load("flame_chase_agent_cleanup").expected_params
    with pytest.raises(ValueError, match="work_paths"):
        model(work_paths=value)


def test_work_paths_read_as_a_flag_gives_them() -> None:
    flow = _load("ralph_loop_agent_cleanup")

    assert flow.params_of({"work_paths": "src"}).work_paths == ("src",)
    assert flow.params_of({"work_paths": "src, lib"}).work_paths == ("src", "lib")
    assert flow.params_of({"work_paths": '["src", "lib"]'}).work_paths == ("src", "lib")
    with pytest.raises(ParamsError):
        flow.params_of({"work_paths": "src,../up"})


@pytest.mark.asyncio
async def test_flame_chase_alternates_retries_an_empty_turn_and_cleans_between_turns(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    name = "flame_chase_agent_cleanup"
    events: list[str] = []
    _cleans(name, events, monkeypatch)
    journal = tmp_path / "journal.jsonl"

    with pytest.raises(CostExceeded):
        await run_fake(
            _load(name),
            "task",
            agents={
                "first_chaser": _speaker("first", events, ["", "done"]),
                "second_chaser": _speaker("second", events),
                "cleaner": _speaker("cleaner", events),
            },
            params={"work_paths": "src"},
            budget=Budget(cost=5),
            local=local(repo),
            journal=journal,
        )

    assert events == ["first", "first", "second", "first", "clean", "second"]
    kept = _kept(journal)
    assert (kept["turns"], kept["epoch"], kept["cleaned_at"]) == (4, 1, 3)
    store = next((tmp_path / "humanize" / name).glob(f"*/{kept['run_id']}"))
    assert (store / "manifest.txt").read_text().splitlines() == [
        "README.md",
        "src/main.py",
    ]


@pytest.mark.asyncio
async def test_ralph_hands_each_due_cleanup_to_its_cleaner(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "ralph_loop_agent_cleanup"
    events: list[str] = []
    _cleans(name, events, monkeypatch)

    with pytest.raises(CostExceeded):
        await run_fake(
            _load(name),
            "task",
            agents={
                "agent": _speaker("coder", events),
                "cleaner": _speaker("cleaner", events),
            },
            params={"work_paths": "src"},
            budget=Budget(cost=4),
            local=local(repo),
        )

    assert events == ["coder", "coder", "coder", "clean", "coder"]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.asyncio
async def test_three_empty_turns_in_a_row_end_the_run_and_keep_its_state(
    name: str, repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _cleans(name, events, monkeypatch)
    journal = tmp_path / "journal.jsonl"

    await run_fake(
        _load(name),
        "task",
        agents={role: _speaker(role, events, [""] * 10) for role in ROLES[name]},
        params={"work_paths": "src"},
        local=local(repo),
        journal=journal,
    )

    assert events == [ROLES[name][0]] * _module(name, "loop").STALLED
    kept = _kept(journal)
    assert kept["turns"] == 0
    assert "run_id" in kept


@pytest.mark.asyncio
async def test_a_turn_the_clock_ended_counts_and_hands_over(repo: Path) -> None:
    events: list[str] = []
    turns = iter(("long", "done"))

    async def first(_prompt: str, *, session: Any, output_schema: Any) -> str:
        events.append("first")
        if next(turns) == "long":
            await session.until_steered()
            deadline = session.requests[-1].limits.deadline
            await asyncio.sleep(max(deadline - time.monotonic(), 0.0) + 0.05)
            return "cut short"
        return "done"

    chaser = FakeAgentDriver(reply=first, cost=1.0)
    with pytest.raises(CostExceeded):
        await run_fake(
            _load("flame_chase_agent_cleanup"),
            "task",
            agents={
                "first_chaser": chaser,
                "second_chaser": _speaker("second", events),
            },
            params={
                "work_paths": "src",
                "session_timeout_minutes": 0.001,
                "idle_timeout_minutes": 0,
                "stop_grace_minutes": 0,
            },
            budget=Budget(cost=2),
            local=local(repo),
        )

    assert events == ["first", "second", "first"]
    session = chaser.sessions[0]
    assert session.requests[0].limits.graceful is False
    assert any("Wrap up" in said for said, _ in session.steered)


@pytest.mark.asyncio
async def test_a_turn_is_cut_off_even_when_the_run_ends_sooner_and_gracefully(
    repo: Path,
) -> None:
    async def endless(_prompt: str, *, session: Any, output_schema: Any) -> str:
        await asyncio.sleep(30)
        return "never"

    coder = FakeAgentDriver(reply=endless)
    began = time.monotonic()
    with pytest.raises(DurationExceeded):
        await run_fake(
            _load("ralph_loop_agent_cleanup"),
            "task",
            agents={"agent": coder},
            params={
                "work_paths": "src",
                "session_timeout_minutes": 0.002,
                "idle_timeout_minutes": 0,
                "stop_grace_minutes": 0.002,
            },
            budget=Budget(duration=dt.timedelta(seconds=0.2)),
            local=local(repo),
        )

    assert time.monotonic() - began < 5
    assert coder.sessions[0].requests[0].limits.graceful is True


@pytest.mark.asyncio
async def test_each_turn_s_session_is_closed_before_the_next_turn(
    repo: Path,
) -> None:
    open_before: list[int] = []

    def reply(_prompt: str, *, session: Any, output_schema: Any) -> str:
        open_before.append(sum(not one.closed for one in session.driver.sessions))
        return "done"

    coder = FakeAgentDriver(reply=reply, cost=1.0)
    with pytest.raises(CostExceeded):
        await run_fake(
            _load("ralph_loop_agent_cleanup"),
            "task",
            agents={"agent": coder},
            params={"work_paths": "src"},
            budget=Budget(cost=3),
            local=local(repo),
        )

    assert open_before == [1, 1, 1]
    assert all(session.closed for session in coder.sessions)


@pytest.mark.asyncio
async def test_a_turn_a_hard_budget_cut_short_still_counts(
    repo: Path, tmp_path: Path
) -> None:
    journal = tmp_path / "journal.jsonl"
    coder = FakeAgentDriver(reply="done", cost=1.0)

    with pytest.raises(CostExceeded):
        await run_fake(
            _load("ralph_loop_agent_cleanup"),
            "task",
            agents={"agent": coder},
            params={"work_paths": "src"},
            budget=Budget(cost=1.5, graceful=False),
            local=local(repo),
            journal=journal,
        )

    assert len(coder.sessions) == 2
    assert _kept(journal)["turns"] == 2


@pytest.mark.asyncio
async def test_a_caller_s_workspace_is_the_one_every_turn_works_in(
    repo: Path, home: Path
) -> None:
    name = "ralph_loop_agent_cleanup"
    ralph = _load(name)
    roles = _module(name, "roles")
    Worker, Workspace = roles.Worker, roles.Workspace  # noqa: N806
    (repo / "pkg" / "src").mkdir(parents=True)
    (repo / "pkg" / "src" / "lib.py").write_text("x = 1\n")

    class Agents(AgentCollection):
        agent: Worker
        cleaner: Worker

    class Envs(EnvCollection):
        workspace: Workspace

    @flow(agents=Agents, envs=Envs, params=FlowParams)
    async def outer(
        task: str, *, agents: Any, envs: Any, params: Any, ctx: Any
    ) -> None:
        pkg = await envs["workspace"].derive_subdir(subdir="pkg")
        await ralph(
            task,
            agents=dict(agents),
            envs={"workspace": pkg},
            params=ralph.expected_params(work_paths=("src",)),
        )

    coder = FakeAgentDriver(cost=1.0)
    with pytest.raises(CostExceeded):
        await run_fake(
            outer,
            "task",
            agents={"agent": coder},
            budget=Budget(cost=1),
            local=local(repo),
        )

    assert coder.sessions
    assert {str(one.placement.workdir) for one in coder.sessions} == {str(repo / "pkg")}
    (manifest,) = home.glob(f"{name}/*/*/manifest.txt")
    assert manifest.read_text().splitlines() == ["src/lib.py"]


@pytest.mark.asyncio
async def test_a_resumed_run_counts_turns_from_its_last_epoch(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    name = "ralph_loop_agent_cleanup"
    events: list[str] = []
    _cleans(name, events, monkeypatch)
    journal = tmp_path / "journal.jsonl"
    agents = {"agent": _speaker("coder", events), "cleaner": _speaker("c", events)}

    with pytest.raises(CostExceeded):
        await run_fake(
            _load(name),
            "task",
            agents=agents,
            params={"work_paths": "src"},
            budget=Budget(cost=3),
            local=local(repo),
            journal=journal,
        )
    kept = _kept(journal)
    assert (kept["turns"], kept["epoch"], kept["cleaned_at"]) == (3, 1, 3)

    events.clear()
    with pytest.raises(CostExceeded):
        await run_fake(
            _load(name),
            "task",
            agents=agents,
            params={"work_paths": "src", "cleanup_turns": 1},
            budget=Budget(cost=2),
            local=local(repo),
            journal=journal,
            resume=True,
        )

    assert events == ["coder", "clean", "coder", "clean"]


@pytest.mark.parametrize("answer", [None, {"answer": "Stop"}])
@pytest.mark.asyncio
async def test_a_large_workspace_is_asked_about_and_not_started_unless_confirmed(
    answer: Any, repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "ralph_loop_agent_cleanup"
    loop = _module(name, "loop")
    monkeypatch.setattr(loop, "FILES_WARNING", 1)
    human = FakeOutworlder(answer, away=answer is None)
    coder = FakeAgentDriver()

    with pytest.raises(loop.LargeWorkspace):
        await run_fake(
            _load(name),
            "task",
            agents={"agent": coder, "cleaner": coder},
            params={"work_paths": "src"},
            outworlder=human,
            local=local(repo),
        )

    if answer is None:
        assert human.asked == []
    else:
        (asked,) = human.asked
        assert "files and" in asked
        assert "Start ralph_loop_agent_cleanup anyway?" in asked
    assert coder.sessions == []
    assert not home.exists()


@pytest.mark.asyncio
async def test_the_question_counts_what_a_revert_point_copies(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "ralph_loop_agent_cleanup"
    loop = _module(name, "loop")
    monkeypatch.setattr(loop, "FILES_WARNING", 1)
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("a\n")
    (root / "b.txt").write_text("b\n")
    human = FakeOutworlder({"answer": "Stop"})

    with pytest.raises(loop.LargeWorkspace):
        await run_fake(
            _load(name),
            "task",
            params={"work_paths": "src"},
            outworlder=human,
            local=local(root),
        )

    assert "holds 2 files" in human.asked[0]


@pytest.mark.parametrize(
    ("answer", "confirm"),
    [({"answer": "Start anyway"}, True), ({"answer": "yes"}, True), (None, False)],
)
@pytest.mark.asyncio
async def test_a_confirmed_or_unasked_large_workspace_starts(
    answer: Any, confirm: bool, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "ralph_loop_agent_cleanup"
    monkeypatch.setattr(_module(name, "loop"), "FILES_WARNING", 1)
    human = FakeOutworlder(answer, away=answer is None)
    events: list[str] = []

    with pytest.raises(CostExceeded):
        await run_fake(
            _load(name),
            "task",
            agents={"agent": _speaker("coder", events)},
            params={"work_paths": "src", "confirm_large_workspace_copies": confirm},
            budget=Budget(cost=1),
            outworlder=human,
            local=local(repo),
        )

    assert events == ["coder"]
    assert len(human.asked) == (1 if confirm else 0)


@pytest.mark.asyncio
async def test_a_resumed_run_is_not_asked_again(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    name = "ralph_loop_agent_cleanup"
    events: list[str] = []
    run = {
        "agents": {"agent": _speaker("coder", events)},
        "params": {"work_paths": "src"},
        "budget": Budget(cost=1),
        "local": local(repo),
        "journal": tmp_path / "journal.jsonl",
    }
    with pytest.raises(CostExceeded):
        await run_fake(_load(name), "task", **run)

    monkeypatch.setattr(_module(name, "loop"), "FILES_WARNING", 0)
    human = FakeOutworlder({"answer": "Stop"})
    with pytest.raises(CostExceeded):
        await run_fake(_load(name), "task", outworlder=human, resume=True, **run)

    assert human.asked == []
    assert events == ["coder", "coder"]


@pytest.mark.asyncio
async def test_run_storage_uses_humanize_home_and_validates_resume(
    repo: Path, home: Path
) -> None:
    storage = _module("flame_chase_agent_cleanup", "storage")
    env = local(repo)
    state: dict[str, Any] = {}

    root, resumed = await storage.open_store("some_flow", env, state)

    assert not resumed
    assert Path(root).is_dir()
    assert Path(root).is_relative_to(home.resolve() / "some_flow")
    assert state["run_id"] == root.name
    assert await storage.open_store("some_flow", env, state) == (root, True)
    with pytest.raises(ValueError):
        await storage.open_store("some_flow", env, {"run_id": "../elsewhere"})
    with pytest.raises(RuntimeError):
        await storage.open_store("some_flow", env, {"run_id": "gone"})


@pytest.mark.asyncio
async def test_run_storage_refuses_humanize_home_inside_cleaned_repository(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _module("flame_chase_agent_cleanup", "storage")
    monkeypatch.setenv("HUMANIZE_HOME", str(repo / ".humanize"))

    with pytest.raises(RuntimeError, match="outside the cleaned repository"):
        await storage.open_store("some_flow", local(repo), {})
    assert not (repo / ".humanize").exists()


@pytest.mark.asyncio
async def test_run_storage_sees_through_a_linked_humanize_home(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _module("flame_chase_agent_cleanup", "storage")
    (tmp_path / "link").symlink_to(repo)
    monkeypatch.setenv("HUMANIZE_HOME", str(tmp_path / "link" / ".humanize"))

    with pytest.raises(RuntimeError, match="outside the cleaned repository"):
        await storage.open_store("some_flow", local(repo), {})
    assert not (repo / ".humanize").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches what is unsearchable")
@pytest.mark.asyncio
async def test_run_storage_under_an_unsearchable_directory_is_refused(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _module("flame_chase_agent_cleanup", "storage")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o600)
    monkeypatch.setenv("HUMANIZE_HOME", str(locked / "home"))

    try:
        with pytest.raises(RuntimeError, match="could not resolve"):
            await storage.open_store("some_flow", local(repo), {})
    finally:
        locked.chmod(0o700)


def _cleaner(
    events: list[str], clean: Callable[[], None], deleted: str = "scratch"
) -> FakeAgentDriver:
    def reply(prompt: str, *, session: Any, output_schema: Any) -> Any:
        events.append("cleaner" if output_schema else "repair")
        clean()
        if output_schema is None:
            return "cut again"
        return {
            "deleted": [deleted],
            "kept": ["src"],
            "check_ran": True,
            "check_passed": True,
        }

    return FakeAgentDriver(reply=reply)


@pytest.mark.asyncio
async def test_ralph_runs_a_whole_epoch_and_reverts_a_cleaning_the_check_fails(
    repo: Path, home: Path
) -> None:
    events: list[str] = []
    written = iter(range(1, 100))

    def code() -> None:
        turn = next(written)
        (repo / "src" / "main.py").write_text(f"value = {turn}  # good, tried {turn}\n")
        (repo / f"notes-{turn}.txt").write_text("what I tried\n")

    def break_it() -> None:
        (repo / "src" / "main.py").write_text("value = broken\n")
        for stray in repo.glob("notes-*.txt"):
            stray.unlink()

    coder = _speaker("coder", events, act=code)
    cleaner = _cleaner(events, break_it)
    with pytest.raises(CostExceeded):
        await run_fake(
            _load("ralph_loop_agent_cleanup"),
            "task",
            agents={"agent": coder, "cleaner": cleaner},
            params={
                "work_paths": "src",
                "cleanup_turns": 2,
                "check_command": "grep -q good src/main.py",
            },
            budget=Budget(cost=3),
            local=local(repo),
        )

    assert events == ["coder", "coder", "cleaner", "coder"]
    assert "You are this repository's cleaner" in cleaner.prompts[0]
    assert "grep -q good src/main.py" in cleaner.prompts[0]
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "log", "-1", "--format=%s").startswith(
        "epoch 1: the tree the coding turns left; the check failed"
    )
    assert _git(repo, "show", "HEAD:src/main.py") == "value = 2  # good, tried 2"
    assert (repo / "src" / "main.py").read_text() == "value = 3  # good, tried 3\n"
    (log,) = home.glob("ralph_loop_agent_cleanup/*/*/checks/epoch-001.log")
    (history,) = home.glob("ralph_loop_agent_cleanup/*/history.git")
    ref = f"refs/runs/{log.parents[1].name}/epoch-001"
    assert _git(repo, "--git-dir", str(history), "log", "--format=%s", ref) == (
        "epoch 1: the tree before cleaning\ntask"
    )


@pytest.mark.asyncio
async def test_flame_chase_distills_the_tree_between_turns(
    repo: Path, home: Path
) -> None:
    events: list[str] = []

    def chase(who: str) -> Callable[[], None]:
        def write() -> None:
            (repo / "src" / f"{who}.py").write_text(f"# tried {who}\nby = '{who}'\n")
            (repo / f"{who}-log.txt").write_text("experiment log\n")
            (repo / "junk").mkdir(exist_ok=True)
            (repo / "junk" / f"{who}.tmp").write_text("x\n")

        return write

    def distill() -> None:
        for who in ("first", "second"):
            (repo / "src" / f"{who}.py").write_text(f"by = '{who}'\n")
            (repo / f"{who}-log.txt").unlink(missing_ok=True)
        (repo / "NEXT.md").write_text("\n".join(f"idea {n}" for n in range(12)) + "\n")

    cleaner = _cleaner(events, distill)
    with pytest.raises(CostExceeded):
        await run_fake(
            _load("flame_chase_agent_cleanup"),
            "task",
            agents={
                "first_chaser": _speaker("first", events, act=chase("first")),
                "second_chaser": _speaker("second", events, act=chase("second")),
                "cleaner": cleaner,
            },
            params={
                "work_paths": "src",
                "cleanup_turns": 2,
                "repairs": 1,
                "check_command": "test -f src/first.py",
            },
            budget=Budget(cost=3),
            local=local(repo),
        )

    assert events == ["first", "second", "cleaner", "repair", "first"]
    assert "2 stray file(s) in junk/ (2)" in cleaner.prompts[1]
    assert "NEXT.md has 12 lines, cap 10" in cleaner.prompts[1]
    assert not (repo / "junk" / "second.tmp").exists()
    assert len((repo / "NEXT.md").read_text().splitlines()) == 10
    assert _git(repo, "log", "--format=%s") == "epoch 1: distilled tree"
    assert set(_git(repo, "ls-files").splitlines()) == {
        "NEXT.md",
        "README.md",
        "src/first.py",
        "src/main.py",
        "src/second.py",
    }
    assert (repo / ".git" / "hooks" / "pre-commit").stat().st_mode & 0o111
    (history,) = home.glob("flame_chase_agent_cleanup/*/history.git")
    distilled = _git(
        repo, "--git-dir", str(history), "for-each-ref", "--format=%(refname)"
    )
    assert any(line.endswith("epoch-001.distilled") for line in distilled.splitlines())
