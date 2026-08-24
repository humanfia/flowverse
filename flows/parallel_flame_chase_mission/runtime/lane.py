"""Mission-only ephemeral fields layered onto a shared lane handle."""

from __future__ import annotations

from dataclasses import dataclass

from _parallel_flame_chase.lanes.runtime import LaneRuntime


@dataclass(slots=True)
class MissionLaneRuntime(LaneRuntime):
    """Track scoped-audit interruption state for one active lane turn."""

    interjected_at: float | None = None
    closed_for_audit: bool = False
    quiesced_revision: int | None = None
