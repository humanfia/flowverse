"""Planner-driven runtime fan-out and synthesis."""

from __future__ import annotations

import asyncio

from hmz.flows import spawn
from pydantic import BaseModel, Field

from .api import Agents, Config


class Plan(BaseModel):
    """Independent work items selected for this task."""

    model_config = {"extra": "forbid"}

    items: list[str] = Field(min_length=1)


async def execute(agents: Agents, task: str, config: Config) -> None:
    """Plan the task, execute its runtime fan-out, and ask for one final answer."""
    plan = await agents.planner.aturn(
        "Split this task into independent work items that can run concurrently. "
        f"Return at most {config.max_workers} items.\n\n{task}",
        schema=Plan,
    )
    if plan is None:
        raise RuntimeError("the planner did not return a work-item plan")
    items = [item.strip() for item in plan.items if item.strip()]
    if not items:
        raise RuntimeError("the planner returned no non-empty work items")
    if len(items) > config.max_workers:
        raise ValueError(
            f"the planner returned {len(items)} work items; maximum is {config.max_workers}"
        )

    workers = spawn(
        agents.worker,
        (f"worker-{index:03d}" for index in range(1, len(items) + 1)),
    )
    results = await asyncio.gather(
        *(
            worker.aturn(f"Complete this work item for the overall task:\n{item}")
            for worker, item in zip(workers, items, strict=True)
        )
    )
    evidence = "\n\n".join(
        f"Work item {index}: {item}\nResult:\n{result}"
        for index, (item, result) in enumerate(zip(items, results, strict=True), 1)
    )
    await agents.synthesizer.aturn(
        f"Produce the final answer to this task:\n{task}\n\nWorker results:\n{evidence}"
    )


__all__ = ["Plan", "execute"]
