"""Internal same-lane structured-report handoff used by Git/PR Lite."""

from __future__ import annotations

from typing import cast

from .core.models import LaneName
from .core.utils import json_copy
from .runtime import ParallelRuntime


class ReportShareRuntime(ParallelRuntime):
    """Inject the immediately preceding same-lane report into each fresh turn."""

    def _previous_lane_report(self, lane: LaneName) -> dict[str, object] | None:
        held = cast("dict[str, object]", self.control["latest_reports"]).get(lane)
        return json_copy(held) if isinstance(held, dict) else None


__all__ = ["ReportShareRuntime"]
