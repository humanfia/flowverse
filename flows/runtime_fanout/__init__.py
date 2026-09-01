"""Plan a task, fan out one runtime agent per work item, then synthesize the result."""

from __future__ import annotations

from _runtime_fanout.api import Agents, Config
from _runtime_fanout.runtime import execute
from hmz.flows import flow


@flow
async def run(agents: Agents, task: str, config: Config | None = None) -> None:
    """Run a planner-selected number of workers in parallel."""
    await execute(agents, task, config or Config())


__all__ = ["Agents", "Config", "run"]
