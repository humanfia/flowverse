from __future__ import annotations

import importlib
import threading
from pathlib import Path
from types import SimpleNamespace

import flame_chase_agent_cleanup as agent_cleanup
import pytest
import ralph_loop_agent_cleanup as ralph_agent_cleanup
from hmz.flows import configures, drives, offered, resumes


class FakeAgent:
    def __init__(
        self, name: str, events: list[str], answers: list[str] | None = None
    ) -> None:
        self.name = name
        self.events = events
        self.answers = list(answers or [])
        self.output = 0

    def spent(self) -> SimpleNamespace:
        return SimpleNamespace(output=self.output)

    def new(self, cwd: str):
        def run(task: str, suppress: bool = False) -> str:
            self.events.append(self.name)
            self.output += 1
            return self.answers.pop(0) if self.answers else "done"

        run.spent = self.spent
        run.interject = lambda _prompt: None
        run.close = lambda: None
        return run


def test_cleanup_flows_are_public_resumable_and_configurable() -> None:
    flows = Path(__file__).parents[1] / "flows"
    agent = flows / "flame_chase_agent_cleanup" / "__init__.py"

    assert drives(agent) == ("first_chaser", "second_chaser", "cleaner")
    assert resumes(agent)
    assert set(configures(agent).model_fields) == {
        "budget",
        "cleanup_turns",
        "work_paths",
        "session_timeout_minutes",
        "idle_timeout_minutes",
        "stop_grace_minutes",
        "next_lines",
        "comment_lines",
        "repairs",
        "check_command",
    }
    offered_names = offered(flows)
    assert "flame_chase_agent_cleanup" in offered_names
    ralph = flows / "ralph_loop_agent_cleanup" / "__init__.py"
    assert drives(ralph) == ("agent", "cleaner")
    assert resumes(ralph)
    assert "ralph_loop_agent_cleanup" in offered_names
    for name in ("flame_chase_agent_cleanup", "ralph_loop_agent_cleanup"):
        config = configures(flows / name / "__init__.py")(work_paths=("src",))
        assert config.cleanup_turns == 3
        assert config.session_timeout_minutes == 240
        assert config.idle_timeout_minutes == 10
        assert config.stop_grace_minutes == 10


def test_agent_cleanup_cleans_after_three_completed_chaser_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    first = FakeAgent("first", events, answers=["", "done"])
    second = FakeAgent("second", events)
    cleaner = FakeAgent("cleaner", events)

    monkeypatch.setattr(agent_cleanup, "home", lambda: tmp_path / "humanize")
    monkeypatch.setattr(agent_cleanup, "_ensure_manifest", lambda *_args: set())
    monkeypatch.setattr(
        agent_cleanup,
        "_clean_epoch",
        lambda *_args: events.append("clean"),
    )
    monkeypatch.setattr(agent_cleanup.time, "sleep", lambda _seconds: None)

    assert agent_cleanup.Config(work_paths=("src",)).cleanup_turns == 3
    assert agent_cleanup.Config(cleanup_turns=4, work_paths=("src",)).cleanup_turns == 4
    agent_cleanup.run(
        agent_cleanup.Agents(first, second, cleaner),
        "task",
        agent_cleanup.Config(budget=0.000005, work_paths=("src",)),
        {},
    )

    assert events == [
        "first",
        "first",
        "second",
        "first",
        "clean",
        "second",
    ]


def test_agent_cleanup_measures_configured_generic_work_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("value = 1\n")
    (root / "README.md").write_text("task\n")
    manifest = set(agent_cleanup._tree_files(root))

    (root / "src" / "new.py").write_text("value = 2  # design intent\n")
    (root / "scratch.txt").write_text("discard me\n")
    (root / "NEXT.md").write_text("try another design\n")

    measured = agent_cleanup._measure(root, manifest, ("src",))

    assert measured.strays == ["scratch.txt"]
    assert measured.notes_lines == 1
    assert measured.comment_count == 1


def test_agent_revert_point_recovers_an_interrupted_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    store = tmp_path / "managed-run"
    root.mkdir()
    store.mkdir()
    (root / "kept.txt").write_text("before\n")

    saved = agent_cleanup._save_tree(root, store)
    (root / "kept.txt").write_text("partly cleaned\n")
    (root / "stray.txt").write_text("partial\n")

    assert agent_cleanup._save_tree(root, store) == saved
    assert (root / "kept.txt").read_text() == "before\n"
    assert not (root / "stray.txt").exists()
    agent_cleanup._drop_saved(saved)
    assert not saved.exists()


@pytest.mark.parametrize("flow", [agent_cleanup, ralph_agent_cleanup])
def test_work_paths_must_be_safe_and_non_overlapping(flow) -> None:
    with pytest.raises(ValueError):
        flow.Config()
    with pytest.raises(ValueError):
        flow.Config(work_paths=("../outside",))
    with pytest.raises(ValueError):
        flow.Config(work_paths=("src", "src/generated"))


@pytest.mark.parametrize("flow", [agent_cleanup, ralph_agent_cleanup])
def test_run_storage_uses_humanize_home_and_validates_resume(
    flow, tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    humanize_home = tmp_path / "managed"
    monkeypatch.setattr(flow, "home", lambda: humanize_home)
    state: dict[str, object] = {}

    root, resumed = flow._open_store(source, state)

    assert not resumed
    assert root.is_relative_to(humanize_home / flow.FLOW_NAME)
    assert state["run_root"] == str(root)
    assert state["run_id"] == root.name
    assert flow._open_store(source, state) == (root, True)
    flow._remove_store(root)


@pytest.mark.parametrize("flow", [agent_cleanup, ralph_agent_cleanup])
def test_run_storage_refuses_humanize_home_inside_cleaned_repository(
    flow, tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    monkeypatch.setattr(flow, "home", lambda: source / ".humanize")

    with pytest.raises(RuntimeError):
        flow._open_store(source, {})


@pytest.mark.parametrize(
    "name", ["flame_chase_agent_cleanup", "ralph_loop_agent_cleanup"]
)
def test_forced_coding_session_retries_same_seat_without_advancing_cleanup(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = importlib.import_module(name)
    events: list[str] = []
    first = FakeAgent("first", events)
    second = FakeAgent("second", events)
    fresh = first.new
    closed = threading.Event()
    sessions = 0

    def new(cwd):
        nonlocal sessions
        sessions += 1
        if sessions > 1:
            assert closed.is_set()
            return fresh(cwd)

        def stuck(*_args, **_kwargs):
            events.append("first")
            first.output += 1
            assert closed.wait(timeout=3)
            return "late answer after close"

        stuck.spent = first.spent
        stuck.interject = lambda _prompt: None
        stuck.close = closed.set
        return stuck

    monkeypatch.setattr(first, "new", new)
    monkeypatch.setattr(flow, "home", lambda: tmp_path / "humanize")
    state = {}

    def after_turn(_seconds):
        assert state["spent"] == first.output + second.output

    monkeypatch.setattr(flow.time, "sleep", after_turn)
    config = {
        "work_paths": ("src",),
        "cleanup_turns": 1,
        "session_timeout_minutes": 0.001,
        "idle_timeout_minutes": 0,
        "stop_grace_minutes": 0,
    }
    is_ralph = name.startswith("ralph_loop")
    monkeypatch.setattr(flow, "_ensure_manifest", lambda *_args: set())
    monkeypatch.setattr(flow, "_clean_epoch", lambda *_args: events.append("cleanup"))
    config["budget"] = 0.000003
    cleaner = FakeAgent("cleaner", events)
    agents = (
        flow.Agents(first, cleaner) if is_ralph else flow.Agents(first, second, cleaner)
    )

    flow.run(agents, "task", flow.Config(**config), state)
    assert events == ["first", "first", "cleanup", "first" if is_ralph else "second"]


@pytest.mark.parametrize("stuck_call", [1, 2])
def test_cleaner_timeout_stops_repairs(
    stuck_call: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = agent_cleanup
    root = tmp_path / "repo"
    store = tmp_path / "store"
    root.mkdir()
    store.mkdir()
    (root / "src.py").write_text("value = 1  # design intent\n")
    closed = threading.Event()
    calls = []
    spending_checks = []

    def session(_prompt, **_kwargs):
        calls.append(_prompt)
        if len(calls) == stuck_call:
            assert closed.wait(timeout=3)
            return None
        return flow.Cleaned(
            deleted=[], kept=["src.py"], check_ran=False, check_passed=False
        )

    session.spent = lambda: SimpleNamespace(total=0)
    session.interject = lambda _prompt: None
    session.close = closed.set
    cleaner = SimpleNamespace(new=lambda **_kwargs: session)
    monkeypatch.setattr(flow, "_erase_history", lambda _root: True)

    flow._clean_epoch(
        cleaner,
        flow.Config(
            work_paths=("src.py",),
            comment_lines=0,
            session_timeout_minutes=0.001,
            idle_timeout_minutes=0,
            stop_grace_minutes=0,
        ),
        root,
        {"src.py"},
        store,
        1,
        lambda: spending_checks.append(True) or False,
    )
    assert closed.is_set()
    assert len(calls) == stuck_call
    assert len(spending_checks) == stuck_call
