"""Contract and runtime behavior of the planner-selected fan-out flow."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from _runtime_fanout.api import Agents, Config
from _runtime_fanout.runtime import Plan, execute
from hmz.flows import configures, drives, offered


class FakeAgent:
    def __init__(self, name: str, *, plan: list[str] | None = None) -> None:
        self.id = name
        self.cycle = None
        self.plan = plan
        self.prompts: list[str] = []

    def clone(self, *, name: str, **_kwargs: Any) -> FakeAgent:
        return FakeAgent(name)

    async def aturn(self, prompt: str, *, schema: Any = None, **_kwargs: Any) -> Any:
        self.prompts.append(prompt)
        if schema is Plan:
            return Plan(items=self.plan or [])
        return f"result from {self.id}"


def test_public_flow_declares_three_persistent_roles() -> None:
    flows = Path(__file__).parents[1] / "flows"
    public = flows / "runtime_fanout" / "__init__.py"
    assert drives(public) == ("planner", "worker", "synthesizer")
    assert configures(public) is not None
    names = offered(flows)
    assert "runtime_fanout" in names
    assert "_runtime_fanout" not in names


def test_the_plan_controls_the_number_of_workers() -> None:
    planner = FakeAgent("planner", plan=["one", "two", "three", "four"])
    worker = FakeAgent("worker")
    synthesizer = FakeAgent("synthesizer")

    asyncio.run(execute(Agents(planner, worker, synthesizer), "do it", Config()))

    assert "worker-001" in synthesizer.prompts[0]
    assert "worker-004" in synthesizer.prompts[0]
