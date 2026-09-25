from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from hmz.flows import BudgetExceeded, FlowCancelled, OutputSchemaError

from ..core.models import InitialPlan, LaneName, LaneReport
from .prompts import lane_repair_prompt

STOPPING = (BudgetExceeded, FlowCancelled)
REPAIRS = 2
PLANNING_ATTEMPTS = 3


@dataclass(slots=True)
class LaneRuntime:
    lane: LaneName
    actors: tuple[Any, Any]
    workspace: Any
    task: asyncio.Task[LaneReport] | None = None
    identity: dict[str, object] = field(default_factory=dict)
    actor_at: int = 0
    pending_ack: dict[str, int] = field(default_factory=dict)
    checkpoint_before: str | None = None


def reason(error: BaseException) -> str:
    """What went wrong, with the causes a harness keeps behind its own message."""
    said = [f"{type(error).__name__}: {error}"]
    cause = error.__cause__
    while cause is not None and len(said) < 4:
        said.append(f"{type(cause).__name__}: {cause}")
        cause = cause.__cause__
    return "\ncaused by ".join(said)


async def run_lane_session(actor: Any, place: Any, prompt: str) -> LaneReport:
    session = await actor.spawn(env=place)
    current = prompt
    for _ in range(REPAIRS):
        try:
            return await actor.run(current, session=session, output_schema=LaneReport)
        except OutputSchemaError as why:
            current = lane_repair_prompt(reason(why)[:2000])
    return await actor.run(current, session=session, output_schema=LaneReport)


async def run_initial_plan(coordinator: Any, place: Any, prompt: str) -> InitialPlan:
    failures: list[str] = []
    for attempt in range(1, PLANNING_ATTEMPTS + 1):
        try:
            session = await coordinator.spawn(env=place)
            return await coordinator.run(
                prompt, session=session, output_schema=InitialPlan
            )
        except STOPPING:
            raise
        except Exception as why:  # noqa: BLE001
            failures.append(f"attempt {attempt}: {reason(why)}"[:1000])
    raise RuntimeError(
        f"initial coordinator failed after {PLANNING_ATTEMPTS} fresh sessions: {failures}"
    )
