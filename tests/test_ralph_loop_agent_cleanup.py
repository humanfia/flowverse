from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import ralph_loop_agent_cleanup as flow


class FakeAgent:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.output = 0

    def spent(self) -> SimpleNamespace:
        return SimpleNamespace(output=self.output)

    def new(self, cwd: str):
        def run(task: str, suppress: bool = False, **kwargs: object) -> str:
            self.events.append(self.name)
            self.output += 1
            return "done"

        run.spent = self.spent
        run.interject = lambda _prompt: None
        run.close = lambda: None
        return run


def test_ralph_uses_configured_cleaner_at_cleanup_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    coder = FakeAgent("coder", events)
    cleaner = FakeAgent("cleaner", events)

    monkeypatch.setattr(flow, "home", lambda: tmp_path / "humanize")
    monkeypatch.setattr(flow, "_ensure_manifest", lambda *_args: set())
    monkeypatch.setattr(flow.time, "sleep", lambda _seconds: None)

    def clean(cleaning_agent, *_args):
        assert cleaning_agent is cleaner
        events.append("cleanup")

    monkeypatch.setattr(flow, "_clean_epoch", clean)

    flow.run(
        flow.Agents(coder, cleaner),
        "task",
        flow.Config(budget=0.000004, work_paths=("src",)),
        {},
    )

    assert events == ["coder", "coder", "coder", "cleanup", "coder"]
