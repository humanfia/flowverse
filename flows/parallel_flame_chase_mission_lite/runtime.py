"""Runtime specialization for best-gated global Mission audits."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Any, cast

from _parallel_flame_chase.core.models import LaneName
from parallel_flame_chase_mission.coordination.controller import MissionController
from parallel_flame_chase_mission.runtime.engine import MissionRuntime


class MissionLiteController(MissionController):
    """Keep terminal lifecycle audits local unless a candidate becomes shared best."""

    def _outcome_audit_request(
        self,
        lane: LaneName,
        status: object,
        *,
        candidate_became_best: bool,
    ) -> tuple[str, list[LaneName]]:
        del status
        if candidate_became_best:
            return "global", []
        return "targeted", [lane]


class MissionLiteRuntime(MissionRuntime):
    """Mission runtime whose deliverables do not automatically stop the fleet."""

    mode_name = "mission-lite"
    skill_name = "parallel-flame-chase-mission-lite"
    orchestrator_role_name = "orchestrateor"
    planning_cadence = (
        "The orchestrateor will return only for scoped audits. A terminal result remains local "
        "unless its candidate refreshes the primary shared best."
    )

    def _make_controller(self, objective: str) -> MissionController:
        return MissionLiteController(
            cast("dict[str, Any] | None", self.control.get("missions")),
            run_id=cast("str", self.control["run_id"]),
            objective=objective,
            global_audit_hours=getattr(self.config, "global_audit_hours", 6.0),
            default_deadline_hours=getattr(self.config, "mission_deadline_hours", 6.0),
            default_max_turns=getattr(self.config, "max_turns_without_outcome", 6),
            clock=self.clock,
        )


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
    MissionLiteRuntime(
        agents,
        task,
        config,
        state,
        clock=_clock,
        sleeper=_sleep,
        max_turns=_max_turns,
    ).run()


__all__ = ["MissionLiteController", "MissionLiteRuntime", "execute"]
