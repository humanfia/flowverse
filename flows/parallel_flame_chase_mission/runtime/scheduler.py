"""Mission state, integration, and lane hooks over the shared base runtime."""

from __future__ import annotations

from typing import Any, cast

from _parallel_flame_chase.core.models import LANES, InitialPlan, LaneName, LaneReport
from _parallel_flame_chase.core.utils import json_copy, now, task_fingerprint
from _parallel_flame_chase.lanes.runtime import LaneRuntime
from _parallel_flame_chase.persistence.workspace import artifacts_still_match
from _parallel_flame_chase.runtime import ParallelRuntime

from ..coordination.controller import MissionController
from .lane import MissionLaneRuntime


class MissionScheduler(ParallelRuntime):
    """Attach mission state and integration semantics to shared lane scheduling."""

    mode_name = "mission"
    skill_name = "parallel-flame-chase-mission"
    planning_cadence = (
        "Every lane mission will later be audited against an explicit outcome."
    )
    replan_on_objective_revision = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.controller: MissionController | None = None

    def _controller(self) -> MissionController:
        if self.controller is None:
            raise RuntimeError("mission controller is not prepared")
        return self.controller

    def _new_mode_control(self) -> dict[str, object]:
        return {
            "missions": None,
            "external": {"cursor": None, "seen_ids": [], "errors": []},
        }

    def _validate_mode_control(self) -> None:
        if not isinstance(self.control.get("missions"), dict):
            raise TypeError("resumable mission state is malformed")
        if not isinstance(self.control.get("external"), dict):
            raise TypeError("resumable external event state is malformed")

    def _prepare_mode(self, objective: str, *, revised: bool) -> None:
        self.controller = self._make_controller(objective)
        if not self.controller.data["missions"]:
            self.controller.bootstrap(InitialPlan.model_validate(self.control["plan"]))
        elif revised:
            current_hash = task_fingerprint(objective)
            revisions = self.controller.data.get("objective_revisions", [])
            if not any(
                item.get("current_fingerprint") == current_hash for item in revisions
            ):
                prior_hash = cast("str", self.state.get("task_fingerprint", ""))
                self.controller.revise_objective(prior_hash, current_hash)
        self.control["missions"] = self.controller.snapshot()

    def _make_controller(self, objective: str) -> MissionController:
        """Construct the mode-owned controller; additive variants may specialize it."""
        return MissionController(
            cast("dict[str, Any] | None", self.control.get("missions")),
            run_id=cast("str", self.control["run_id"]),
            objective=objective,
            global_audit_hours=getattr(self.config, "global_audit_hours", 6.0),
            default_deadline_hours=getattr(self.config, "mission_deadline_hours", 6.0),
            default_max_turns=getattr(self.config, "max_turns_without_outcome", 6),
            clock=self.clock,
        )

    def _before_persist(self) -> None:
        if self.controller is not None:
            self.control["missions"] = self.controller.snapshot()

    def _manifest_fields(self) -> dict[str, object]:
        controller = self._controller()
        external = cast("dict[str, Any]", self.control["external"])
        return {
            "active_audit": json_copy(controller.active_audit()),
            "integration_queue": json_copy(
                controller.data.get("integration_queue", [])
            ),
            "external_ingress": {
                "configured": bool(getattr(self.config, "external_events", None)),
                "cursor": json_copy(external.get("cursor")),
                "recent_errors": json_copy(external.get("errors", [])[-10:]),
            },
        }

    def _make_lane_runtime(self, **fields: Any) -> LaneRuntime:
        return MissionLaneRuntime(**fields)

    def _identity(self, lane: LaneName) -> dict[str, object]:
        return self._controller().identity(lane)

    def _lane_context(self, lane: LaneName) -> tuple[bool, dict[str, object] | None]:
        controller = self._controller()
        if controller.auditing(lane):
            return False, None
        mission = controller.current_mission(lane)
        if mission.get("status") != "active":
            return False, None
        if lane == "lane-1":
            self._activate_queued_integration()
            if controller.auditing(lane):
                return False, None
            mission = controller.current_mission(lane)
        return True, json_copy(mission)

    def _integration_item(self, lane: LaneName) -> dict[str, object] | None:
        if lane != "lane-1":
            return None
        controller = self._controller()
        mission = controller.current_mission(lane)
        if cast("dict[str, Any]", mission["spec"]).get("kind") != "integration":
            return None
        queue = cast(
            "list[dict[str, object]]",
            controller.data["integration_queue"],
        )
        item = next(
            (
                held
                for held in queue
                if held.get("integration_mission_id") == mission["id"]
            ),
            None,
        )
        return json_copy(item)

    def _activate_queued_integration(self) -> bool:
        """Validate and activate the next handoff at Lane 1's natural boundary."""
        controller = self._controller()
        if controller.active_audit() is not None:
            return False
        current = controller.current_mission("lane-1")
        if cast("dict[str, Any]", current["spec"]).get("kind") == "integration":
            return False
        item = controller.queued_integration()
        if item is None:
            return False
        source_lane = item.get("source_lane")
        artifacts = item.get("artifacts")
        if source_lane not in LANES or not isinstance(artifacts, list):
            controller.invalidate_integration(
                cast("str", item["id"]), "malformed package"
            )
            self._persist()
            return True
        root = self.paths.artifact_root(cast("LaneName", source_lane))
        if not artifacts_still_match(root, cast("list[dict[str, object]]", artifacts)):
            controller.invalidate_integration(
                cast("str", item["id"]),
                "artifact package changed after acceptance",
            )
            self._persist()
            return True
        controller.activate_integration(cast("str", item["id"]))
        self._persist()
        return True

    def _mission_lane(self, runtime: LaneRuntime) -> MissionLaneRuntime:
        if not isinstance(runtime, MissionLaneRuntime):
            raise TypeError("Mission mode requires MissionLaneRuntime")
        return runtime

    def _after_lane_scheduled(self, runtime: LaneRuntime) -> None:
        mission = self._mission_lane(runtime)
        mission.interjected_at = None
        mission.closed_for_audit = False
        mission.quiesced_revision = None

    def _observe_report(
        self,
        runtime: LaneRuntime,
        record: dict[str, object],
        report: LaneReport,
        *,
        candidate_became_best: bool,
    ) -> None:
        self._controller().observe(
            runtime.lane,
            cast("dict[str, Any]", record),
            candidate_became_best=candidate_became_best,
        )

    def _handle_pair_failure(self, runtime: LaneRuntime, error: str) -> None:
        self._controller().trigger(
            "actor_pair_blocked",
            {
                "event_id": (
                    f"{runtime.identity.get('mission_id')}:actor-pair:"
                    f"{runtime.identity.get('generation')}"
                ),
                "at": now(),
                "lane": runtime.lane,
                "error": error[:2000],
            },
            scope="targeted",
            targets=[runtime.lane],
        )

    def _stopped_turn_error(self, runtime: LaneRuntime) -> str | None:
        return (
            "session closed by scoped audit"
            if self._mission_lane(runtime).closed_for_audit
            else None
        )
