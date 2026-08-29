"""Report-driven runtime with explicit same-lane partner handoff."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Any, cast

from _parallel_flame_chase.core.models import LaneName
from _parallel_flame_chase.core.utils import json_copy
from _parallel_flame_chase.runtime import ParallelRuntime


class ReportShareRuntime(ParallelRuntime):
    """Inject the immediately preceding same-lane report into each fresh turn."""

    mode_name = "report-share"
    skill_name = "parallel-flame-chase-report-share"
    orchestrator_role_name = "orchestrateor"
    planning_cadence = (
        "This is the only orchestrateor turn. Alternating lane partners will receive the latest "
        "same-lane report and must independently validate it."
    )

    def _previous_lane_report(self, lane: LaneName) -> dict[str, object] | None:
        held = cast("dict[str, object]", self.control["latest_reports"]).get(lane)
        return json_copy(held) if isinstance(held, dict) else None


def execute(
    agents: Any,
    task: str,
    config: Any,
    state: dict[str, Any] | None,
    *,
    _clock: Callable[[], dt.datetime] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    _max_turns: int | None = None,
) -> None:
    ReportShareRuntime(
        agents,
        task,
        config,
        state,
        clock=_clock,
        sleeper=_sleep,
        max_turns=_max_turns,
    ).run()


__all__ = ["ReportShareRuntime", "execute"]
