from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import flame_chase_agent_cleanup as agent_cleanup
import flame_chase_rule_cleanup as rule_cleanup
import pytest
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

        return run


def test_cleanup_flows_are_public_resumable_and_configurable() -> None:
    flows = Path(__file__).parents[1] / "flows"
    rule = flows / "flame_chase_rule_cleanup" / "__init__.py"
    agent = flows / "flame_chase_agent_cleanup" / "__init__.py"

    assert drives(rule) == ("flame", "chaser")
    assert drives(agent) == ("first_chaser", "second_chaser", "cleaner")
    assert resumes(rule)
    assert resumes(agent)
    assert set(configures(rule).model_fields) == {
        "budget_millions",
        "cleanup_turns",
        "work_paths",
    }
    assert set(configures(agent).model_fields) == {
        "budget",
        "cleanup_turns",
        "work_paths",
        "next_lines",
        "comment_lines",
        "repairs",
        "check_command",
    }
    offered_names = offered(flows)
    assert "flame_chase_rule_cleanup" in offered_names
    assert "flame_chase_agent_cleanup" in offered_names


def test_rule_cleanup_runs_after_five_completed_agent_turns(
    monkeypatch,
) -> None:
    events: list[str] = []
    first = FakeAgent("first", events)
    second = FakeAgent("second", events)

    monkeypatch.setattr(rule_cleanup, "ensure_snapshot", lambda *_args: False)
    monkeypatch.setattr(
        rule_cleanup,
        "cleanup",
        lambda *_args: events.append("cleanup") or (0, ("src",), 0),
    )
    monkeypatch.setattr(rule_cleanup.time, "sleep", lambda _seconds: None)

    assert rule_cleanup.Config(work_paths=("src",)).cleanup_turns == 5
    assert rule_cleanup.Config(cleanup_turns=4, work_paths=("src",)).cleanup_turns == 4
    rule_cleanup.run(
        rule_cleanup.Agents(first, second),
        "task",
        rule_cleanup.Config(budget_millions=0.000006, work_paths=("src",)),
        {},
    )

    assert events == [
        "first",
        "second",
        "first",
        "second",
        "first",
        "cleanup",
        "second",
    ]


def test_agent_cleanup_cleans_after_five_completed_chaser_turns(
    monkeypatch,
) -> None:
    events: list[str] = []
    first = FakeAgent("first", events, answers=["", "done"])
    second = FakeAgent("second", events)
    cleaner = FakeAgent("cleaner", events)

    monkeypatch.setattr(agent_cleanup, "_ensure_manifest", lambda _root: set())
    monkeypatch.setattr(
        agent_cleanup,
        "_clean_epoch",
        lambda *_args: events.append("clean"),
    )
    monkeypatch.setattr(agent_cleanup.time, "sleep", lambda _seconds: None)

    assert agent_cleanup.Config(work_paths=("src",)).cleanup_turns == 5
    assert agent_cleanup.Config(cleanup_turns=4, work_paths=("src",)).cleanup_turns == 4
    agent_cleanup.run(
        agent_cleanup.Agents(first, second, cleaner),
        "task",
        agent_cleanup.Config(budget=0.000007, work_paths=("src",)),
        {},
    )

    assert events == [
        "first",
        "first",
        "second",
        "first",
        "second",
        "first",
        "clean",
        "second",
    ]


def test_rule_cleanup_preserves_configured_generic_work_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    keep = tmp_path / "state"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("value = 1\n")
    (root / "project.toml").write_text("mode = 'initial'\n")
    (root / "README.md").write_text("task\n")
    assert rule_cleanup.ensure_snapshot(root, keep)

    (root / "src" / "main.py").write_text("value = 2  # experiment\n")
    (root / "src" / "new.py").write_text("# attempt\nvalue = 3\n")
    (root / "project.toml").write_text("mode = 'current'\n")
    (root / "README.md").write_text("agent note\n")
    (root / "scratch.txt").write_text("discard me\n")

    removed, carried, stripped = rule_cleanup.cleanup(
        root, keep, (Path("src"), Path("project.toml"))
    )

    assert removed >= 5
    assert carried == ("src", "project.toml")
    assert stripped == 2
    assert "#" not in (root / "src" / "main.py").read_text()
    assert "#" not in (root / "src" / "new.py").read_text()
    assert (root / "project.toml").read_text() == "mode = 'current'\n"
    assert (root / "README.md").read_text() == "task\n"
    assert not (root / "scratch.txt").exists()
    commits = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert commits.stdout.strip() == "1"


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


@pytest.mark.parametrize("flow", [rule_cleanup, agent_cleanup], ids=["rule", "agent"])
def test_work_paths_must_be_safe_and_non_overlapping(flow) -> None:
    with pytest.raises(ValueError):
        flow.Config()
    with pytest.raises(ValueError):
        flow.Config(work_paths=("../outside",))
    with pytest.raises(ValueError):
        flow.Config(work_paths=("src", "src/generated"))
