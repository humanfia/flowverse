"""Runtime for orchestrateor-planned audit control."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from _parallel_flame_chase.core.utils import close_safely, json_copy
from hmz.flows import Stopped
from parallel_flame_chase_mission.coordination.controller import MissionController
from parallel_flame_chase_mission.runtime.engine import MissionRuntime

from parallel_flame_chase_mission_control.controller import ControlledMissionController
from parallel_flame_chase_mission_control.models import AuditPolicy, MissionControlPlan
from parallel_flame_chase_mission_control.prompts import control_planning_prompt


class MissionControlRuntime(MissionRuntime):
    """Persist one first-start audit policy and apply it to every trigger."""

    mode_name = "mission-control"
    skill_name = "parallel-flame-chase-mission-control"
    orchestrator_role_name = "orchestrateor"
    planning_cadence = (
        "The orchestrateor must also establish the durable audit-control policy."
    )

    def _new_mode_control(self) -> dict[str, object]:
        return {**super()._new_mode_control(), "audit_policy": None}

    def _validate_mode_control(self) -> None:
        super()._validate_mode_control()
        AuditPolicy.model_validate(self.control.get("audit_policy"))

    def _runtime_defaults(self) -> dict[str, object]:
        return {
            "experiment_time_budget_hours": getattr(
                self.config, "experiment_time_budget_hours", None
            ),
            "fallback_mission_deadline_hours": getattr(
                self.config, "mission_deadline_hours", 6.0
            ),
            "fallback_max_turns_without_outcome": getattr(
                self.config, "max_turns_without_outcome", 6
            ),
            "audit_interrupt_grace_seconds": getattr(
                self.config, "interrupt_grace_seconds", 60.0
            ),
            "external_event_stream_configured": bool(
                getattr(self.config, "external_events", None)
            ),
        }

    def _control_plan(self, objective: str, cwd: Path) -> MissionControlPlan:
        prompt = control_planning_prompt(
            objective=objective,
            workspace_map=self._workspace_map(),
            runtime_defaults=self._runtime_defaults(),
            skill=self.skill_name,
        )
        failures: list[str] = []
        for attempt in range(1, 4):
            session = self.agents.coordinator.new(cwd=cwd)
            try:
                result = session(prompt, suppress=False, schema=MissionControlPlan)
            except Stopped:
                raise
            except Exception as why:  # noqa: BLE001 - preserve backend diagnostics
                failures.append(
                    f"attempt {attempt}: {type(why).__name__}: {why}"[:1000]
                )
                result = None
            finally:
                close_safely(session)
            if result is not None:
                return result
            if len(failures) < attempt:
                failures.append(
                    f"attempt {attempt}: orchestrateor returned no structured control plan"
                )
        raise RuntimeError(
            f"initial orchestrateor failed after 3 fresh sessions: {failures}"
        )

    def _prepare_plan(self, objective: str, *, resume: bool, revised: bool) -> None:
        del revised
        if resume and self.control.get("plan") is not None:
            return
        result = self._control_plan(objective, self.paths.planning)
        self.control["plan"] = result.plan.model_dump(mode="json")
        self.control["audit_policy"] = result.audit_policy.model_dump(mode="json")

    def _make_controller(self, objective: str) -> MissionController:
        policy = AuditPolicy.model_validate(self.control["audit_policy"])
        periodic_hours = (
            None
            if policy.action_for("periodic_review") == "off"
            else policy.periodic_review_hours
        )
        return ControlledMissionController(
            cast("dict[str, Any] | None", self.control.get("missions")),
            run_id=cast("str", self.control["run_id"]),
            objective=objective,
            global_audit_hours=periodic_hours,
            default_deadline_hours=getattr(self.config, "mission_deadline_hours", 6.0),
            default_max_turns=getattr(self.config, "max_turns_without_outcome", 6),
            clock=self.clock,
            audit_policy=policy,
        )

    def _manifest_fields(self) -> dict[str, object]:
        fields = super()._manifest_fields()
        controller = self.controller
        suppressed = (
            controller.data.get("suppressed_audits", [])[-20:]
            if controller is not None
            else []
        )
        return {
            **fields,
            "audit_policy": json_copy(self.control.get("audit_policy")),
            "recent_suppressed_audits": json_copy(suppressed),
        }


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
    MissionControlRuntime(
        agents,
        task,
        config,
        state,
        clock=_clock,
        sleeper=_sleep,
        max_turns=_max_turns,
    ).run()


__all__ = ["MissionControlRuntime", "execute"]
