from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import ralph_loop_workspace_cleanup as flow


class FakeAgent:
    def __init__(self, events: list[str], answers: list[str] | None = None) -> None:
        self.events = events
        self.answers = list(answers or [])
        self.output = 0

    def spent(self) -> SimpleNamespace:
        return SimpleNamespace(output=self.output)

    def new(self, cwd: str):
        def run(task: str, suppress: bool = False) -> str:
            self.events.append("turn")
            self.output += 1
            return self.answers.pop(0) if self.answers else "done"

        run.spent = self.spent
        run.interject = lambda _prompt: None
        run.close = lambda: None
        return run


def test_ralph_cleanup_runs_one_fresh_agent_and_cleans_at_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    agent = FakeAgent(events)
    monkeypatch.setattr(flow, "home", lambda: tmp_path / "humanize")
    monkeypatch.setattr(flow, "ensure_snapshot", lambda *_args: False)
    monkeypatch.setattr(
        flow, "cleanup", lambda *_args: events.append("cleanup") or (0, ("src",), 0)
    )
    monkeypatch.setattr(flow.time, "sleep", lambda _seconds: None)

    flow.run(
        flow.Agents(agent),
        "task",
        flow.Config(budget=0.000004, work_paths=("src",)),
        {},
    )

    assert events == ["turn", "turn", "turn", "cleanup", "turn"]


def test_cleanup_preserves_only_configured_paths_and_restarts_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    store = tmp_path / "store"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("value = 1\n")
    (root / "README.md").write_text("task\n")
    flow.ensure_snapshot(root, store)

    (root / "src" / "main.py").write_text("value = 2  # attempt\n")
    (root / "scratch.txt").write_text("discard\n")
    removed, carried, stripped = flow.cleanup(root, store, (Path("src"),))

    assert removed >= 3
    assert carried == ("src",)
    assert stripped == 1
    assert "#" not in (root / "src" / "main.py").read_text()
    assert (root / "README.md").read_text() == "task\n"
    assert not (root / "scratch.txt").exists()
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "1"


def test_work_paths_are_safe_and_non_overlapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError):
        flow.Config()
    with pytest.raises(ValueError):
        flow.Config(work_paths=("../outside",))
    with pytest.raises(ValueError):
        flow.Config(work_paths=("src", "src/generated"))
