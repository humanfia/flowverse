"""Audit-policy-aware Mission controller."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from _parallel_flame_chase.core.models import LANES, LaneName
from _parallel_flame_chase.core.utils import json_copy
from parallel_flame_chase_mission.coordination.controller import MissionController

from parallel_flame_chase_mission_control.models import (
    AuditAction,
    AuditCondition,
    AuditPolicy,
)


class ControlledMissionController(MissionController):
    """Apply the initial orchestrateor's policy before opening any audit."""

    def __init__(self, *args: Any, audit_policy: AuditPolicy, **kwargs: Any) -> None:
        self.audit_policy = audit_policy
        super().__init__(*args, **kwargs)
        suppressed = self.data.setdefault("suppressed_audits", [])
        if not isinstance(suppressed, list) or any(
            not isinstance(item, dict) for item in suppressed
        ):
            raise TypeError("suppressed audit history is malformed")

    @staticmethod
    def _conditions(kind: str, event: dict[str, object]) -> list[AuditCondition]:
        if kind == "mission_outcome":
            held: list[AuditCondition] = []
            if event.get("candidate_became_best") is True:
                held.append("shared_best_updated")
            outcome = event.get("outcome")
            if outcome in {"deliverable_ready", "no_result", "blocked"}:
                held.append(cast("AuditCondition", outcome))
            return held
        direct: dict[str, AuditCondition] = {
            "deliverable_ready": "deliverable_ready",
            "no_result": "no_result",
            "blocked": "blocked",
            "turn_stall": "turn_stall",
            "mission_deadline": "mission_deadline",
            "actor_pair_blocked": "actor_pair_blocked",
            "invalid_deliverable": "invalid_deliverable",
            "review_requested": "external_review_requested",
            "objective_revision": "objective_revision",
            "periodic_global_review": "periodic_review",
        }
        condition = direct.get(kind)
        return [condition] if condition is not None else []

    @staticmethod
    def _targeted_lanes(
        event: dict[str, object], targets: Sequence[LaneName]
    ) -> list[LaneName]:
        lane = event.get("lane")
        if lane in LANES:
            return [cast("LaneName", lane)]
        return [candidate for candidate in LANES if candidate in targets]

    def _request(
        self,
        action: AuditAction,
        event: dict[str, object],
        *,
        original_scope: str,
        original_targets: Sequence[LaneName],
    ) -> tuple[str, list[LaneName]] | None:
        if action == "off":
            return None
        if action == "global":
            return "global", []
        if action == "original":
            return original_scope, list(original_targets)
        targeted = self._targeted_lanes(event, original_targets)
        return ("targeted", targeted) if targeted else None

    def _record_suppressed(
        self,
        kind: str,
        event: dict[str, object],
        conditions: list[AuditCondition],
    ) -> None:
        history = cast("list[dict[str, object]]", self.data["suppressed_audits"])
        history.append(
            {
                "at": self._at(),
                "kind": kind,
                "conditions": list(conditions),
                "event": json_copy(event),
                "reason": "disabled by the persisted Mission Control audit policy",
            }
        )
        del history[:-200]

    def _continue_after_suppression(self, kind: str, event: dict[str, object]) -> None:
        lane = event.get("lane")
        if lane not in LANES:
            if kind == "periodic_global_review":
                self.data["last_global_trigger_at"] = self._at()
                self._refresh_global_clock()
            return
        mission = self.current_mission(cast("LaneName", lane))
        if event.get("mission_id") not in {None, mission.get("id")}:
            return
        if kind == "mission_outcome" and mission.get("status") in {
            "completed",
            "blocked",
        }:
            mission["last_suppressed_outcome"] = json_copy(event)
            self._reset(mission, "terminal audit suppressed by Mission Control policy")
        elif kind == "turn_stall":
            mission["turns_since_dispatch"] = 0
            mission["deadline_at"] = self._deadline(
                cast("dict[str, Any]", mission["spec"])
            )
            mission["updated_at"] = self._at()
        elif kind == "mission_deadline":
            mission["deadline_at"] = self._deadline(
                cast("dict[str, Any]", mission["spec"])
            )
            mission["updated_at"] = self._at()

    def trigger(
        self,
        kind: str,
        event: dict[str, object],
        *,
        scope: str,
        targets: Sequence[LaneName] = (),
    ) -> str | None:
        conditions = self._conditions(kind, event)
        if not conditions:
            return super().trigger(kind, event, scope=scope, targets=targets)

        requests = [
            request
            for condition in conditions
            if (
                request := self._request(
                    self.audit_policy.action_for(condition),
                    event,
                    original_scope=scope,
                    original_targets=targets,
                )
            )
            is not None
        ]
        if not requests:
            self._record_suppressed(kind, event, conditions)
            self._continue_after_suppression(kind, event)
            return None

        resolved_scope = (
            "global" if any(held[0] == "global" for held in requests) else "targeted"
        )
        resolved_targets = (
            []
            if resolved_scope == "global"
            else [
                lane
                for lane in LANES
                if any(lane in held_targets for _, held_targets in requests)
            ]
        )
        governed = {
            **json_copy(event),
            "mission_control": {
                "conditions": list(conditions),
                "actions": {
                    condition: self.audit_policy.action_for(condition)
                    for condition in conditions
                },
            },
        }
        return super().trigger(
            kind,
            governed,
            scope=resolved_scope,
            targets=resolved_targets,
        )


__all__ = ["ControlledMissionController"]
