"""Domain-neutral mission state and scoped audit transitions."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any, cast

from _parallel_flame_chase.core.models import (
    LANES,
    InitialPlan,
    LaneName,
    MissionSpec,
)
from _parallel_flame_chase.core.utils import json_copy, parse_time

from ..audits.runtime import decision_error
from .models import (
    AcceptDecision,
    AuditDecision,
    ExternalEventV1,
    IntegrationDirective,
)

MISSION_PROTOCOL = 1
MISSION_STATES = {
    "planned",
    "active",
    "paused",
    "completed",
    "blocked",
    "accepted",
    "rejected",
}


def _require_unique_ids(records: list[dict[str, Any]], label: str) -> None:
    identifiers = [record.get("id") for record in records]
    if any(not isinstance(identifier, str) for identifier in identifiers) or len(
        set(identifiers)
    ) != len(identifiers):
        raise ValueError(f"{label} contain missing or duplicate IDs")


class MissionController:
    """The single-writer state machine for missions, audits, and integration work."""

    def __init__(
        self,
        data: dict[str, Any] | None,
        *,
        run_id: str,
        objective: str,
        global_audit_hours: float | None,
        default_deadline_hours: float,
        default_max_turns: int,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.global_audit_hours = global_audit_hours
        self.default_deadline_hours = default_deadline_hours
        self.default_max_turns = default_max_turns
        if data:
            if data.get("version") != 1 or data.get("protocol") != MISSION_PROTOCOL:
                raise ValueError("unsupported parallel mission state")
            if data.get("run_id") != run_id:
                raise ValueError("mission state belongs to another run")
            self.data = data
            self.data["objective"] = objective
            self._validate_loaded()
        else:
            stamp = self._at()
            self.data = {
                "version": 1,
                "protocol": MISSION_PROTOCOL,
                "run_id": run_id,
                "objective": objective,
                "created_at": stamp,
                "updated_at": stamp,
                "initial_dispatched_at": None,
                "last_global_trigger_at": None,
                "last_targeted_trigger_at": None,
                "next_global_audit_at": None,
                "global_generation": 0,
                "lane_generations": {lane: 0 for lane in LANES},
                "current": {},
                "missions": [],
                "outcomes": [],
                "audits": [],
                "active_audit": None,
                "integration_queue": [],
                "objective_revisions": [],
            }

    def _validate_loaded(self) -> None:
        """Validate the relationships needed to resume the state machine safely."""
        for name, kind in (
            ("missions", list),
            ("outcomes", list),
            ("audits", list),
            ("integration_queue", list),
            ("current", dict),
            ("lane_generations", dict),
        ):
            if not isinstance(self.data.get(name), kind):
                raise TypeError(f"mission state field {name!r} has an invalid shape")
        for name in ("missions", "audits", "integration_queue"):
            if any(not isinstance(item, dict) for item in self.data[name]):
                raise TypeError(f"mission state field {name!r} must contain objects")

        missions = self._missions()
        _require_unique_ids(missions, "missions")
        for mission in missions:
            if mission.get("status") not in MISSION_STATES:
                raise ValueError(f"mission {mission.get('id')} has invalid status")
            if mission.get("lane") not in LANES:
                raise ValueError(f"mission {mission.get('id')} has invalid lane")
            MissionSpec.model_validate(mission.get("spec"))

        current = self._validate_current()
        self._validate_audits()
        self._validate_queue(current)

    def _validate_current(self) -> dict[str, object]:
        """Require one current mission and generation counter per lane."""
        current = cast("dict[str, object]", self.data["current"])
        if set(current) != set(LANES):
            raise ValueError(
                "mission state must name one current mission for every lane"
            )
        for lane in LANES:
            mission = self._mission(current[lane])
            if mission is None or mission.get("lane") != lane:
                raise ValueError(
                    f"{lane} current mission is missing or belongs to another lane"
                )
        generations = cast("dict[str, object]", self.data["lane_generations"])
        if set(generations) != set(LANES) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in generations.values()
        ):
            raise ValueError(
                "lane generations must be non-negative integers for all lanes"
            )
        return current

    def _validate_audits(self) -> None:
        """Require exactly one owned unfinished audit, or none."""
        audits = self._audits()
        _require_unique_ids(audits, "audits")
        for audit in audits:
            if audit.get("status") not in {"quiescing", "deciding", "completed"}:
                raise ValueError(f"audit {audit.get('id')} has invalid status")
            targets = audit.get("targets")
            if (
                not isinstance(targets, list)
                or not targets
                or len(set(targets)) != len(targets)
                or any(target not in LANES for target in targets)
            ):
                raise ValueError(f"audit {audit.get('id')} has invalid targets")
        active = self.data.get("active_audit")
        unfinished = [audit for audit in audits if audit.get("status") != "completed"]
        if active is not None:
            audit = next((item for item in audits if item.get("id") == active), None)
            if audit is None or audit.get("status") == "completed":
                raise ValueError("active_audit does not name an unfinished audit")
            if len(unfinished) != 1 or unfinished[0].get("id") != active:
                raise ValueError("mission state contains an orphaned unfinished audit")
        elif unfinished:
            raise ValueError("mission state contains an unfinished audit with no owner")

    def _validate_queue(self, current: dict[str, object]) -> None:
        """Require a coherent, single active handoff into Lane 1."""
        queue = self._queue()
        _require_unique_ids(queue, "integration queue items")
        for item in queue:
            if item.get("status") not in {"queued", "active", "accepted", "rejected"}:
                raise ValueError(
                    f"integration item {item.get('id')} has invalid status"
                )
            if item.get("source_lane") not in {"lane-2", "lane-3"}:
                raise ValueError(
                    f"integration item {item.get('id')} has invalid source lane"
                )
        active_items = [item for item in queue if item.get("status") == "active"]
        if len(active_items) > 1:
            raise ValueError("more than one integration queue item is active")
        if active_items:
            lane_one = self._mission(current["lane-1"])
            if (
                lane_one is None
                or active_items[0].get("integration_mission_id") != lane_one.get("id")
                or cast("dict[str, Any]", lane_one.get("spec", {})).get("kind")
                != "integration"
            ):
                raise ValueError("active integration item does not match Lane 1")

    def _time(self) -> dt.datetime:
        return self._clock().astimezone(dt.UTC)

    def _at(self, moment: dt.datetime | None = None) -> str:
        current = (moment or self._time()).astimezone(dt.UTC)
        return current.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _next_id(self, prefix: str, collection: Sequence[dict[str, Any]]) -> str:
        largest = 0
        for item in collection:
            value = item.get("id")
            if isinstance(value, str) and value.startswith(prefix):
                try:
                    largest = max(largest, int(value[len(prefix) :]))
                except ValueError:
                    continue
        return f"{prefix}{largest + 1:06d}"

    def _missions(self) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self.data["missions"])

    def _audits(self) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self.data["audits"])

    def _queue(self) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self.data["integration_queue"])

    def _mission(self, mission_id: object) -> dict[str, Any] | None:
        if not isinstance(mission_id, str):
            return None
        return next(
            (item for item in self._missions() if item.get("id") == mission_id), None
        )

    def current_mission(self, lane: LaneName) -> dict[str, Any]:
        mission_id = cast("dict[str, str]", self.data["current"]).get(lane)
        mission = self._mission(mission_id)
        if mission is None:
            raise RuntimeError(f"{lane} has no current mission")
        return mission

    def generation(self, lane: LaneName) -> int:
        raw = cast("dict[str, object]", self.data["lane_generations"]).get(lane, 0)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0

    def identity(self, lane: LaneName) -> dict[str, object]:
        mission = self.current_mission(lane)
        return {
            "version": 1,
            "run_id": self.data["run_id"],
            "lane": lane,
            "mission_id": mission["id"],
            "generation": self.generation(lane),
        }

    def snapshot(self) -> dict[str, Any]:
        self.data["updated_at"] = self._at()
        return cast("dict[str, Any]", json_copy(self.data))

    def bootstrap(self, plan: InitialPlan) -> None:
        """Create exactly three initial missions once."""
        if self._missions() or cast("dict[str, str]", self.data["current"]):
            raise RuntimeError("missions are already initialized")
        stamp = self._at()
        by_lane = {brief.lane: brief.mission for brief in plan.lanes}
        for lane in LANES:
            self._create_mission(lane, by_lane[lane], "initial coordinator plan", stamp)
        self.data["initial_dispatched_at"] = stamp
        self._refresh_global_clock()

    def _deadline(
        self, spec: MissionSpec | dict[str, Any], start: str | None = None
    ) -> str:
        raw = (
            spec.get("deadline_hours")
            if isinstance(spec, dict)
            else spec.deadline_hours
        )
        hours = (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else self.default_deadline_hours
        )
        base = parse_time(start) or self._time()
        return self._at(base + dt.timedelta(hours=hours))

    def _create_mission(
        self,
        lane: LaneName,
        spec: MissionSpec,
        reason: str,
        stamp: str | None = None,
        *,
        parents: Sequence[str] = (),
        resume_mission_id: str | None = None,
    ) -> dict[str, Any]:
        at = stamp or self._at()
        mission = {
            "id": self._next_id("M", self._missions()),
            "lane": lane,
            "spec": spec.model_dump(mode="json"),
            "status": "active",
            "turns_since_dispatch": 0,
            "deadline_at": self._deadline(spec, at),
            "outcome": None,
            "parents": list(parents),
            "resume_mission_id": resume_mission_id,
            "created_at": at,
            "updated_at": at,
            "transitions": [
                {"at": at, "from": None, "to": "planned", "reason": reason},
                {"at": at, "from": "planned", "to": "active", "reason": "dispatched"},
            ],
            "audits": [],
        }
        self._missions().append(mission)
        cast("dict[str, str]", self.data["current"])[lane] = cast("str", mission["id"])
        return mission

    def _transition(self, mission: dict[str, Any], target: str, reason: str) -> None:
        if target not in MISSION_STATES:
            raise ValueError(f"invalid mission status {target}")
        source = mission.get("status")
        if source == target:
            return
        at = self._at()
        mission["status"] = target
        mission["updated_at"] = at
        cast("list[dict[str, object]]", mission["transitions"]).append(
            {"at": at, "from": source, "to": target, "reason": reason}
        )

    def active_audit(self) -> dict[str, Any] | None:
        audit_id = self.data.get("active_audit")
        return next(
            (item for item in self._audits() if item.get("id") == audit_id), None
        )

    @staticmethod
    def _ordered(values: Sequence[object]) -> list[LaneName]:
        return [lane for lane in LANES if lane in values]

    def _refresh_global_clock(self) -> None:
        if self.global_audit_hours is None:
            self.data["next_global_audit_at"] = None
            return
        reference = parse_time(self.data.get("last_global_trigger_at")) or parse_time(
            self.data.get("initial_dispatched_at")
        )
        if reference is None:
            self.data["next_global_audit_at"] = None
            return
        self.data["next_global_audit_at"] = self._at(
            reference + dt.timedelta(hours=self.global_audit_hours)
        )

    def trigger(
        self,
        kind: str,
        event: dict[str, object],
        *,
        scope: str,
        targets: Sequence[LaneName] = (),
    ) -> str:
        """Open one audit or attach and escalate decision-bearing evidence."""
        if scope not in {"targeted", "global"}:
            raise ValueError(f"invalid audit scope {scope}")
        requested = list(LANES) if scope == "global" else self._ordered(targets)
        if not requested:
            raise ValueError("targeted audit requires at least one lane")
        at = self._at()
        audit = self.active_audit()
        if audit is not None:
            events = cast("list[dict[str, object]]", audit["trigger_events"])
            event_id = event.get("event_id")
            appended = not any(item.get("event_id") == event_id for item in events)
            if appended:
                events.append(json_copy(event))
            prior_scope = audit["scope"]
            prior_targets = list(cast("list[LaneName]", audit["targets"]))
            if scope == "global":
                audit["scope"] = "global"
                audit["targets"] = list(LANES)
            elif audit["scope"] != "global":
                audit["targets"] = self._ordered([*prior_targets, *requested])
            changed = (
                appended
                or prior_scope != audit["scope"]
                or prior_targets != audit["targets"]
            )
            if changed:
                audit["revision"] = int(audit.get("revision", 0)) + 1
                audit["status"] = "quiescing"
                audit["updated_at"] = at
                audit["target_generations"] = {
                    lane: self.generation(lane)
                    for lane in cast("list[LaneName]", audit["targets"])
                }
            if prior_scope != "global" and audit["scope"] == "global":
                self.data["last_global_trigger_at"] = at
                self._refresh_global_clock()
            elif scope == "targeted":
                self.data["last_targeted_trigger_at"] = at
            return cast("str", audit["id"])
        audit_id = self._next_id("A", self._audits())
        audit = {
            "id": audit_id,
            "scope": scope,
            "targets": requested,
            "revision": 0,
            "status": "quiescing",
            "trigger_kind": kind,
            "trigger_events": [json_copy(event)],
            "target_generations": {lane: self.generation(lane) for lane in requested},
            "requested_at": at,
            "updated_at": at,
            "quiesced": {},
            "evidence": {},
            "decision_attempts": [],
            "decision": None,
            "dispatch": None,
            "completed_at": None,
        }
        self._audits().append(audit)
        self.data["active_audit"] = audit_id
        if scope == "global":
            self.data["last_global_trigger_at"] = at
            self._refresh_global_clock()
        else:
            self.data["last_targeted_trigger_at"] = at
        return audit_id

    def targets(self) -> tuple[LaneName, ...]:
        audit = self.active_audit()
        if audit is None:
            return ()
        return tuple(cast("list[LaneName]", audit["targets"]))

    def auditing(self, lane: LaneName) -> bool:
        return lane in self.targets()

    def observe(self, lane: LaneName, report: dict[str, Any]) -> None:
        """Advance one active mission from a validated actor report."""
        mission = self.current_mission(lane)
        if report.get("mission_id") != mission["id"] or report.get(
            "generation"
        ) != self.generation(lane):
            return
        if mission.get("status") != "active":
            return
        status = report.get("status")
        if status in {"turn_failed", None}:
            return
        if status == "progress":
            turns = int(mission.get("turns_since_dispatch", 0)) + 1
            mission["turns_since_dispatch"] = turns
            mission["updated_at"] = self._at()
            spec = cast("dict[str, Any]", mission["spec"])
            maximum = int(spec.get("max_turns_without_outcome", self.default_max_turns))
            if turns >= maximum:
                self.trigger(
                    "turn_stall",
                    {
                        "event_id": f"{mission['id']}:turn-stall:{turns}",
                        "at": self._at(),
                        "lane": lane,
                        "mission_id": mission["id"],
                        "turns": turns,
                        "maximum": maximum,
                    },
                    scope="targeted",
                    targets=[lane],
                )
            return
        event: dict[str, object] = {
            "event_id": f"{mission['id']}:{status}",
            "at": self._at(),
            "lane": lane,
            "mission_id": mission["id"],
            "outcome": status,
            "summary": report.get("summary"),
            "evidence": report.get("evidence", []),
            "deliverable": report.get("deliverable"),
            "artifacts": report.get("artifacts", []),
        }
        mission["outcome"] = json_copy(event)
        cast("list[dict[str, object]]", self.data["outcomes"]).append(json_copy(event))
        self._transition(
            mission,
            "blocked" if status == "blocked" else "completed",
            f"lane reported {status}",
        )
        self.trigger(
            "mission_outcome",
            event,
            scope="global"
            if status == "deliverable_ready" and lane == "lane-1"
            else "targeted",
            targets=[]
            if status == "deliverable_ready" and lane == "lane-1"
            else [lane],
        )

    def observe_external(self, event: ExternalEventV1) -> None:
        """Attach a validated adapter event without granting it decision authority."""
        document = event.model_dump(mode="json")
        kind = event.kind
        if kind == "progress":
            self.data["last_external_progress"] = document
            return
        if kind == "review_requested":
            self.trigger(
                kind,
                document,
                scope=cast("str", event.scope),
                targets=event.targets,
            )
            return
        lane = event.lane
        if lane is None:  # Model validation normally makes this unreachable.
            raise ValueError(f"{kind} requires lane")
        scope = (
            "global" if kind == "deliverable_ready" and lane == "lane-1" else "targeted"
        )
        self.trigger(
            kind, document, scope=scope, targets=[] if scope == "global" else [lane]
        )

    def revise_objective(self, prior_hash: str, current_hash: str) -> None:
        """Preserve compatible work but force a fleet-wide replan on TASK revision."""
        event: dict[str, object] = {
            "event_id": f"objective:{current_hash}",
            "at": self._at(),
            "prior_fingerprint": prior_hash,
            "current_fingerprint": current_hash,
        }
        cast("list[dict[str, object]]", self.data["objective_revisions"]).append(event)
        self.trigger("objective_revision", event, scope="global")

    def tick(self) -> None:
        """Open due deadline audits and the periodic global portfolio audit."""
        current = self._time()
        for lane in LANES:
            mission = self.current_mission(lane)
            if mission.get("status") != "active":
                continue
            deadline = parse_time(mission.get("deadline_at"))
            if deadline is not None and current >= deadline:
                self.trigger(
                    "mission_deadline",
                    {
                        "event_id": f"{mission['id']}:deadline:{mission['deadline_at']}",
                        "at": self._at(current),
                        "lane": lane,
                        "mission_id": mission["id"],
                        "deadline_at": mission["deadline_at"],
                    },
                    scope="targeted",
                    targets=[lane],
                )
        due = parse_time(self.data.get("next_global_audit_at"))
        if due is not None and current >= due:
            self.trigger(
                "periodic_global_review",
                {
                    "event_id": f"global-period:{self._at(due)}",
                    "at": self._at(current),
                    "reference_at": self._at(due),
                },
                scope="global",
            )

    def mark_quiesced(
        self,
        lane: LaneName,
        interruption: dict[str, object],
        evidence: dict[str, object],
    ) -> None:
        audit = self.active_audit()
        if audit is None or lane not in audit["targets"]:
            return
        cast("dict[str, object]", audit["quiesced"])[lane] = json_copy(interruption)
        cast("dict[str, object]", audit["evidence"])[lane] = json_copy(evidence)
        audit["updated_at"] = self._at()

    def ready_for_decision(self) -> bool:
        audit = self.active_audit()
        if audit is None:
            return False
        targets = set(cast("list[LaneName]", audit["targets"]))
        quiesced = set(cast("dict[str, object]", audit["quiesced"]))
        return targets <= quiesced

    def deciding(self) -> tuple[str, int] | None:
        audit = self.active_audit()
        if audit is None or not self.ready_for_decision():
            return None
        audit["status"] = "deciding"
        audit["updated_at"] = self._at()
        return cast("str", audit["id"]), int(audit["revision"])

    def record_attempt(self, attempt: dict[str, object]) -> None:
        audit = self.active_audit()
        if audit is None:
            return
        cast("list[dict[str, object]]", audit["decision_attempts"]).append(
            json_copy(attempt)
        )
        audit["updated_at"] = self._at()

    def decision_packet(
        self,
        latest_reports: dict[str, object],
        manifest: dict[str, object],
    ) -> dict[str, object]:
        audit = self.active_audit()
        if audit is None:
            raise RuntimeError("no active audit")
        current = cast("dict[str, str]", self.data["current"])
        active_missions = {
            lane: json_copy(self._mission(current.get(lane))) for lane in LANES
        }
        return {
            "version": 1,
            "protocol": MISSION_PROTOCOL,
            "run_id": self.data["run_id"],
            "objective": self.data["objective"],
            "audit": json_copy(audit),
            "active_missions": active_missions,
            "integration_queue": json_copy(self._queue()),
            "last_external_progress": json_copy(
                self.data.get("last_external_progress")
            ),
            "latest_reports": json_copy(latest_reports),
            "recent_audits": json_copy(self._audits()[-10:]),
            "manifest": json_copy(manifest),
        }

    def validate_decision(self, decision: AuditDecision) -> str | None:
        """Return a precise repair message, never a partially applicable decision."""
        audit = self.active_audit()
        if audit is None:
            return "the audit is no longer active"
        missions = {lane: self.current_mission(lane) for lane in LANES}
        return decision_error(audit, missions, decision)

    def _reset(self, mission: dict[str, Any], reason: str) -> None:
        self._transition(mission, "active", reason)
        mission["turns_since_dispatch"] = 0
        mission["deadline_at"] = self._deadline(cast("dict[str, Any]", mission["spec"]))
        mission["outcome"] = None

    def _enqueue(
        self,
        source_mission: dict[str, Any],
        decision: AcceptDecision,
    ) -> dict[str, Any]:
        outcome = cast("dict[str, Any]", source_mission["outcome"])
        directive = cast("IntegrationDirective", decision.integration)
        item = {
            "id": self._next_id("I", self._queue()),
            "status": "queued",
            "source_lane": source_mission["lane"],
            "source_mission_id": source_mission["id"],
            "priority": directive.priority,
            "directive": directive.model_dump(mode="json"),
            "deliverable": json_copy(outcome["deliverable"]),
            "artifacts": json_copy(outcome["artifacts"]),
            "accepted_at": self._at(),
            "integration_mission_id": None,
            "completed_at": None,
            "reason": None,
        }
        self._queue().append(item)
        return item

    def apply(self, decision: AuditDecision) -> tuple[LaneName, ...]:
        """Atomically apply one validated decision and advance only target generations."""
        invalid = self.validate_decision(decision)
        if invalid is not None:
            raise ValueError(invalid)
        audit = cast("dict[str, Any]", self.active_audit())
        ordered = sorted(decision.lanes, key=lambda item: LANES.index(item.lane))
        for item in ordered:
            mission = self.current_mission(item.lane)
            cast("list[str]", mission["audits"]).append(cast("str", audit["id"]))
            if item.verdict == "continue":
                self._reset(mission, f"continued by audit {audit['id']}: {item.reason}")
            elif item.verdict == "redirect":
                self._transition(
                    mission,
                    "rejected",
                    f"redirected by audit {audit['id']}: {item.reason}",
                )
                integration = (
                    item.lane == "lane-1"
                    and cast("dict[str, Any]", mission["spec"]).get("kind")
                    == "integration"
                )
                replacement = self._create_mission(
                    item.lane,
                    item.replacement,
                    f"replacement from audit {audit['id']}: {item.reason}",
                    parents=[cast("str", mission["id"])],
                    resume_mission_id=(
                        cast("str | None", mission.get("resume_mission_id"))
                        if integration and item.replacement.kind == "integration"
                        else None
                    ),
                )
                if integration:
                    queue_item = next(
                        (
                            queued
                            for queued in self._queue()
                            if queued.get("integration_mission_id") == mission["id"]
                        ),
                        None,
                    )
                    if queue_item is not None:
                        if item.replacement.kind == "integration":
                            queue_item["integration_mission_id"] = replacement["id"]
                        else:
                            queue_item["status"] = "rejected"
                            queue_item["reason"] = item.reason
                            queue_item["completed_at"] = self._at()
                    if item.replacement.kind != "integration":
                        paused = self._mission(mission.get("resume_mission_id"))
                        if paused is not None and paused.get("status") == "paused":
                            self._transition(
                                paused,
                                "rejected",
                                f"superseded after failed integration {mission['id']}",
                            )
            else:
                accepted = cast("AcceptDecision", item)
                self._transition(
                    mission,
                    "accepted",
                    f"accepted by audit {audit['id']}: {item.reason}",
                )
                if item.lane != "lane-1":
                    self._enqueue(mission, accepted)
                    self._create_mission(
                        item.lane,
                        cast("MissionSpec", accepted.next_mission),
                        f"successor after accepted mission {mission['id']}",
                        parents=[cast("str", mission["id"])],
                    )
                else:
                    self._finish_lane_one_accept(mission, accepted)
            generations = cast("dict[str, int]", self.data["lane_generations"])
            generations[item.lane] = self.generation(item.lane) + 1
        if audit["scope"] == "global":
            self.data["global_generation"] = (
                int(self.data.get("global_generation", 0)) + 1
            )
        audit["status"] = "completed"
        audit["decision"] = decision.model_dump(mode="json")
        audit["completed_at"] = self._at()
        audit["dispatch"] = {
            "mode": "fresh-session-after-audit",
            "lanes": [item.lane for item in ordered],
            "generations": {item.lane: self.generation(item.lane) for item in ordered},
        }
        self.data["active_audit"] = None
        return tuple(item.lane for item in ordered)

    def _finish_lane_one_accept(
        self,
        mission: dict[str, Any],
        decision: AcceptDecision,
    ) -> None:
        kind = cast("dict[str, Any]", mission["spec"]).get("kind")
        if kind == "integration":
            queue_item = next(
                (
                    item
                    for item in self._queue()
                    if item.get("integration_mission_id") == mission["id"]
                ),
                None,
            )
            if queue_item is not None:
                queue_item["status"] = "accepted"
                queue_item["completed_at"] = self._at()
            resume = self._mission(mission.get("resume_mission_id"))
            if decision.next_mission is None and resume is not None:
                self._reset(resume, f"resumed after integration {mission['id']}")
                cast("dict[str, str]", self.data["current"])["lane-1"] = cast(
                    "str", resume["id"]
                )
                return
            if resume is not None and resume.get("status") == "paused":
                self._transition(
                    resume, "rejected", f"replaced after integration {mission['id']}"
                )
        self._create_mission(
            "lane-1",
            cast("MissionSpec", decision.next_mission),
            f"successor after accepted mission {mission['id']}",
            parents=[cast("str", mission["id"])],
        )

    def queued_integration(self) -> dict[str, Any] | None:
        queued = [item for item in self._queue() if item.get("status") == "queued"]
        if not queued:
            return None
        return min(
            queued,
            key=lambda item: (
                int(item.get("priority", 50)),
                str(item.get("accepted_at", "")),
            ),
        )

    def activate_integration(self, item_id: str) -> dict[str, Any]:
        """Preempt Lane 1 only at a natural boundary and preserve its research mission."""
        item = next(
            (
                candidate
                for candidate in self._queue()
                if candidate.get("id") == item_id
            ),
            None,
        )
        if item is None or item.get("status") != "queued":
            raise ValueError(f"integration item {item_id} is not queued")
        current = self.current_mission("lane-1")
        if cast("dict[str, Any]", current["spec"]).get("kind") == "integration":
            raise RuntimeError("Lane 1 already has an active integration mission")
        self._transition(current, "paused", f"preempted by integration {item_id}")
        directive = cast("dict[str, Any]", item["directive"])
        deliverable = cast("dict[str, Any]", item["deliverable"])
        spec = MissionSpec(
            title=f"Integrate {deliverable['title']}",
            objective=cast("str", directive["objective"]),
            success_criteria=cast("list[str]", directive["success_criteria"]),
            kind="integration",
            approach_class=f"integration:{deliverable['approach_class']}",
            change_scale="integration",
            information_question="Does the accepted deliverable improve the integrated source without regressions?",
            dependencies=[cast("str", item["source_mission_id"])],
            deadline_hours=self.default_deadline_hours,
            max_turns_without_outcome=self.default_max_turns,
        )
        mission = self._create_mission(
            "lane-1",
            spec,
            f"accepted integration queue item {item_id}",
            parents=[cast("str", item["source_mission_id"])],
            resume_mission_id=cast("str", current["id"]),
        )
        item["status"] = "active"
        item["integration_mission_id"] = mission["id"]
        item["started_at"] = self._at()
        generations = cast("dict[str, int]", self.data["lane_generations"])
        generations["lane-1"] = self.generation("lane-1") + 1
        return item

    def invalidate_integration(self, item_id: str, reason: str) -> None:
        item = next(
            (
                candidate
                for candidate in self._queue()
                if candidate.get("id") == item_id
            ),
            None,
        )
        if item is None:
            return
        item["status"] = "rejected"
        item["reason"] = reason
        item["completed_at"] = self._at()
        source = item.get("source_lane")
        if source in LANES:
            self.trigger(
                "invalid_deliverable",
                {
                    "event_id": f"{item_id}:invalid",
                    "at": self._at(),
                    "lane": source,
                    "integration_item": item_id,
                    "reason": reason,
                },
                scope="targeted",
                targets=self._ordered([source]),
            )
