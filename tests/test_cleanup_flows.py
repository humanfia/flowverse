from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import flame_chase_agent_cleanup as agent_cleanup
import flame_chase_rule_cleanup as rule_cleanup
from hmz.flows import configures, drives, offered, resumes


class FakeAgent:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.output = 0

    def spent(self) -> SimpleNamespace:
        return SimpleNamespace(output=self.output)

    def new(self, cwd: str):
        def run(task: str, suppress: bool = False) -> str:
            self.events.append(self.name)
            self.output += 1
            return "done"

        return run


def test_cleanup_flows_are_public_resumable_and_configurable() -> None:
    flows = Path(__file__).parents[1] / "flows"
    rule = flows / "flame_chase_rule_cleanup" / "__init__.py"
    agent = flows / "flame_chase_agent_cleanup" / "__init__.py"

    assert drives(rule) == ("flame", "chaser")
    assert drives(agent) == ("first_chaser", "second_chaser", "cleaner")
    assert resumes(rule)
    assert resumes(agent)
    assert set(configures(rule).model_fields) == {"budget_millions", "wipe_turns"}
    assert set(configures(agent).model_fields) == {
        "budget",
        "clean_turns",
        "next_lines",
        "comment_lines",
        "repairs",
        "check_command",
    }
    offered_names = offered(flows)
    assert "flame_chase_rule_cleanup" in offered_names
    assert "flame_chase_agent_cleanup" in offered_names


def test_rule_cleanup_wipes_after_five_completed_agent_turns(
    monkeypatch,
) -> None:
    events: list[str] = []
    first = FakeAgent("first", events)
    second = FakeAgent("second", events)

    monkeypatch.setattr(rule_cleanup, "ensure_snapshot", lambda *_args: False)
    monkeypatch.setattr(
        rule_cleanup,
        "wipe",
        lambda *_args: events.append("wipe") or (0, True, 0),
    )
    monkeypatch.setattr(rule_cleanup.time, "sleep", lambda _seconds: None)

    assert rule_cleanup.Config().wipe_turns == 5
    assert rule_cleanup.Config(wipe_turns=4).wipe_turns == 4
    rule_cleanup.run(
        rule_cleanup.Agents(first, second),
        "task",
        rule_cleanup.Config(budget_millions=0.000006),
        {},
    )

    assert events == [
        "first",
        "second",
        "first",
        "second",
        "first",
        "wipe",
        "second",
    ]


def test_agent_cleanup_cleans_after_five_completed_chaser_turns(
    monkeypatch,
) -> None:
    events: list[str] = []
    first = FakeAgent("first", events)
    second = FakeAgent("second", events)
    cleaner = FakeAgent("cleaner", events)

    monkeypatch.setattr(agent_cleanup, "_ensure_manifest", lambda _root: set())
    monkeypatch.setattr(
        agent_cleanup,
        "_clean_epoch",
        lambda *_args: events.append("clean"),
    )
    monkeypatch.setattr(agent_cleanup.time, "sleep", lambda _seconds: None)

    assert agent_cleanup.Config().clean_turns == 5
    assert agent_cleanup.Config(clean_turns=4).clean_turns == 4
    agent_cleanup.run(
        agent_cleanup.Agents(first, second, cleaner),
        "task",
        agent_cleanup.Config(budget=0.000006),
        {},
    )

    assert events == [
        "first",
        "second",
        "first",
        "second",
        "first",
        "clean",
        "second",
    ]
