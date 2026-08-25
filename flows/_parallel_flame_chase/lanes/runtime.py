"""Ephemeral lane handles and one-session report repair."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path

from hmz.flows import Agent, Session, Stopped

from ..core.models import LaneName, LaneReport
from .prompts import lane_repair_prompt


@dataclass(slots=True)
class LaneRuntime:
    """Ephemeral handles for one lane; durable facts live in flow state."""

    lane: LaneName
    actors: tuple[Agent, Agent]
    workspace: Path
    future: Future[LaneReport | None] | None = None
    session: Session | None = None
    identity: dict[str, object] = field(default_factory=dict)
    actor_at: int = 0
    pending_ack: dict[str, int] = field(default_factory=dict)
    checkpoint_before: tuple[int, int, str] | None = None


def run_lane_session(session: Session, prompt: str) -> LaneReport | None:
    """Repair report-shape mistakes in the same actor session."""
    current = prompt
    for number in range(1, 4):
        try:
            report = session(current, suppress=False, schema=LaneReport)
        except Stopped:
            raise
        except ValueError as why:
            if number == 3:
                raise
            current = lane_repair_prompt(f"{type(why).__name__}: {why}"[:2000])
            continue
        if report is not None:
            return report
        if number < 3:
            current = lane_repair_prompt("the actor returned no structured report")
    return None
