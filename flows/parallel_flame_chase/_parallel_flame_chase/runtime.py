"""Single-writer orchestration for base and mission-aware parallel Flame Chases."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from hmz.flows import Agent, Session, Stopped, home

from .missions import MissionController
from .models import (
    LANES,
    AcceptDecision,
    AuditDecision,
    InitialPlan,
    LaneCheckpoint,
    LaneName,
    LaneReport,
)
from .prompts import audit_prompt, audit_repair_prompt, lane_prompt, planning_prompt
from .storage import (
    ExternalEventReader,
    ReportBus,
    RunPaths,
    SourceLock,
    artifacts_still_match,
    atomic_json,
    atomic_text,
    initialize_paths,
    inspect_workspace,
    now,
    snapshot,
    task_fingerprint,
    tree_fingerprint,
    validate_deliverable,
    validate_runtime_layout,
    workspace_key,
)

STATE_VERSION = 1
PROTOCOL_VERSION = 1
CHECKPOINT_FILE_LIMIT = 1024 * 1024
CONTINUATION_MARKERS = {
    "continue",
    "continue.",
    "resume",
    "resume.",
    "go on",
    "继续",
    "继续。",
}


@dataclass(slots=True)
class LaneRuntime:
    """Ephemeral handles for one lane; durable facts live in the flow state."""

    lane: LaneName
    actors: tuple[Agent, Agent]
    workspace: Path
    future: Future[LaneReport | None] | None = None
    session: Session | None = None
    identity: dict[str, object] = field(default_factory=dict)
    actor_at: int = 0
    pending_ack: dict[str, dict[str, object]] = field(default_factory=dict)
    checkpoint_before: tuple[int, int, str] | None = None
    interjected_at: float | None = None
    closed_for_audit: bool = False
    quiesced_revision: int | None = None


@dataclass(slots=True)
class CoordinatorRuntime:
    """One fresh audit session and the revision it was asked to decide."""

    future: Future[tuple[AuditDecision | None, list[dict[str, object]]]] | None = None
    session: Session | None = None
    audit_id: str | None = None
    revision: int | None = None
    retry_count: int = 0
    retry_after: float = 0.0
    closed_for_stale_revision: bool = False


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _checkpoint_fingerprint(path: Path) -> tuple[int, int, str] | None:
    if path.is_symlink() or not path.is_file():
        return None
    info = path.stat()
    digest = (
        "oversized"
        if info.st_size > CHECKPOINT_FILE_LIMIT
        else hashlib.sha256(path.read_bytes()).hexdigest()
    )
    return info.st_size, info.st_mtime_ns, digest


def _checkpoint_report(
    path: Path,
    before: tuple[int, int, str] | None,
    expected: Mapping[str, object],
) -> LaneReport | None:
    """Recover a changed, exact-identity checkpoint after an interrupted turn."""
    if _checkpoint_fingerprint(path) == before:
        return None
    try:
        if path.stat().st_size > CHECKPOINT_FILE_LIMIT:
            return None
        checkpoint = LaneCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    identity = checkpoint.identity.model_dump(mode="json")
    for key in ("version", "run_id", "lane", "mission_id", "generation"):
        if identity.get(key) != expected.get(key):
            return None
    return checkpoint.report


def _semantic_decision_error(
    packet: Mapping[str, object], decision: AuditDecision
) -> str | None:
    """Validate a decision inside a worker without reading mutable controller state."""
    raw_audit = packet.get("audit")
    raw_missions = packet.get("active_missions")
    if not isinstance(raw_audit, dict) or not isinstance(raw_missions, dict):
        return "the evidence packet is malformed"
    audit = cast("dict[str, Any]", raw_audit)
    missions = cast("dict[str, Any]", raw_missions)
    if decision.audit_id != audit.get("id"):
        return f"audit_id must be {audit.get('id')}"
    if decision.revision != audit.get("revision"):
        return f"revision must be {audit.get('revision')}"
    targets = audit.get("targets")
    if not isinstance(targets, list):
        return "the audit target list is malformed"
    named = [item.lane for item in decision.lanes]
    if len(named) != len(targets) or set(named) != set(targets):
        return f"decisions must name exactly {targets}"
    for item in decision.lanes:
        mission = missions.get(item.lane)
        if not isinstance(mission, dict):
            return f"{item.lane} has no active mission in the packet"
        spec = mission.get("spec")
        kind = spec.get("kind") if isinstance(spec, dict) else None
        if item.verdict != "accept":
            continue
        accepted = cast("AcceptDecision", item)
        outcome = mission.get("outcome")
        if (
            not isinstance(outcome, dict)
            or outcome.get("outcome") != "deliverable_ready"
        ):
            return f"{item.lane} accept requires a deliverable_ready mission outcome"
        if item.lane != "lane-1":
            if accepted.integration is None or accepted.next_mission is None:
                return f"{item.lane} accept requires integration and next_mission"
            if not outcome.get("deliverable") or not outcome.get("artifacts"):
                return f"{item.lane} accepted deliverable has no validated artifact package"
        elif accepted.integration is not None:
            return "lane-1 accept must not enqueue work to itself"
        elif kind != "integration" and accepted.next_mission is None:
            return "lane-1 non-integration accept requires next_mission"
        elif (
            kind == "integration"
            and accepted.next_mission is None
            and not mission.get("resume_mission_id")
        ):
            return "lane-1 integration accept needs a successor when nothing is paused"
    return None


def _run_audit_session(
    session: Session,
    packet: dict[str, object],
) -> tuple[AuditDecision | None, list[dict[str, object]]]:
    """Try one proposal and two same-session repairs before yielding no decision."""
    attempts: list[dict[str, object]] = []
    prompt = audit_prompt(packet)
    for number in range(1, 4):
        try:
            proposed = session(prompt, suppress=True, schema=AuditDecision)
        except Stopped:
            raise
        except Exception as why:  # noqa: BLE001 - backend failures are an open set
            error = f"{type(why).__name__}: {why}"[:2000]
            attempts.append({"number": number, "at": now(), "error": error})
            return None, attempts
        if proposed is None:
            error = "coordinator returned no structured decision"
        else:
            error = _semantic_decision_error(packet, proposed)
            if error is None:
                attempts.append({"number": number, "at": now(), "valid": True})
                return proposed, attempts
        attempts.append({"number": number, "at": now(), "error": error})
        prompt = audit_repair_prompt(error, packet)
    return None, attempts


class ParallelRuntime:
    """Own all mutable control state while model turns execute in worker threads."""

    def __init__(
        self,
        agents: Any,
        task: str,
        config: Any,
        state: dict[str, Any] | None,
        *,
        mission_mode: bool,
        clock: Callable[[], dt.datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_turns: int | None = None,
    ) -> None:
        self.agents = agents
        self.raw_task = task
        self.config = config
        self.state = state if state is not None else {}
        self.mission_mode = mission_mode
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.sleeper = sleeper
        self.max_turns = max_turns
        self.source = Path.cwd().resolve()
        self.control: dict[str, Any] = {}
        self.paths: RunPaths
        self.bus: ReportBus
        self.controller: MissionController | None = None
        self.lanes: dict[LaneName, LaneRuntime] = {}
        self.coordinator = CoordinatorRuntime()
        self.executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="parallel-flame"
        )
        self.completed_turns = 0

    def _resolve_objective(self) -> tuple[str, bool, bool]:
        """Return objective, resume-existing, and objective-revised flags."""
        if (
            self.state.get("version") == STATE_VERSION
            and self.state.get("protocol") != PROTOCOL_VERSION
        ):
            raise ValueError("unsupported parallel Flame Chase state protocol")
        marker = self.raw_task.strip().casefold() in CONTINUATION_MARKERS
        previous = self.state if self.state.get("version") == STATE_VERSION else {}
        previous_source = previous.get("source") == str(self.source)
        forced_fresh = getattr(self.config, "resume_mode", "auto") == "fresh"
        task_file = self.source / "TASK.md"
        if marker:
            if task_file.is_file():
                objective = task_file.read_text(encoding="utf-8").strip()
            elif previous_source and isinstance(previous.get("objective"), str):
                objective = cast("str", previous["objective"]).strip()
            else:
                raise ValueError("continue/resume requires TASK.md or resumable state")
            if not objective:
                raise ValueError("the resumed objective is empty")
            if (
                forced_fresh
                or not previous_source
                or previous.get("mode") != self._mode
            ):
                return objective, False, False
            prior = previous.get("task_fingerprint")
            revised = isinstance(prior, str) and prior != task_fingerprint(objective)
            return objective, True, revised
        objective = self.raw_task.strip()
        if not objective:
            raise ValueError("task must not be empty")
        fingerprint = task_fingerprint(objective)
        resume = (
            not forced_fresh
            and previous_source
            and previous.get("mode") == self._mode
            and previous.get("task_fingerprint") == fingerprint
        )
        return objective, resume, False

    @property
    def _mode(self) -> str:
        return "mission" if self.mission_mode else "base"

    def _validate_resumable_control(self) -> None:
        """Reject partial or cross-protocol state instead of guessing missing control facts."""
        if self.control.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("unsupported parallel Flame Chase state protocol")
        if self.control.get("mode") != self._mode:
            raise ValueError("resumable state belongs to another flow mode")
        if (
            not isinstance(self.control.get("run_id"), str)
            or not self.control["run_id"]
        ):
            raise ValueError("resumable state has no run_id")
        InitialPlan.model_validate(self.control.get("plan"))
        lanes = self.control.get("lanes")
        if not isinstance(lanes, dict) or set(lanes) != set(LANES):
            raise ValueError("resumable state must contain exactly three lanes")
        for lane in LANES:
            held = lanes[lane]
            if not isinstance(held, dict):
                raise TypeError(f"{lane} resumable state is malformed")
            next_actor = held.get("next_actor")
            if (
                not isinstance(next_actor, int)
                or isinstance(next_actor, bool)
                or next_actor not in {0, 1}
            ):
                raise ValueError(f"{lane} next_actor must be 0 or 1")
            for name in ("turns", "consecutive_failures"):
                value = held.get(name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"{lane} {name} must be a non-negative integer")
        for name in (
            "bus_cursors",
            "report_heads",
            "latest_reports",
            "protected_baselines",
            "external",
        ):
            if not isinstance(self.control.get(name), dict):
                raise TypeError(f"resumable state field {name!r} is malformed")
        if not isinstance(self.control.get("events"), list):
            raise TypeError("resumable state events are malformed")

    def _new_control(self, objective: str) -> dict[str, Any]:
        stamp = self.clock().astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{uuid.uuid4().hex[:10]}"
        root = home() / "parallel_flame_chase" / workspace_key(self.source) / run_id
        return {
            "version": STATE_VERSION,
            "protocol": PROTOCOL_VERSION,
            "mode": self._mode,
            "run_id": run_id,
            "run_root": str(root),
            "source": str(self.source),
            "objective": objective,
            "task_fingerprint": task_fingerprint(objective),
            "status": "starting",
            "created_at": now(),
            "updated_at": now(),
            "plan": None,
            "lanes": {
                lane: {
                    "next_actor": 0,
                    "turns": 0,
                    "consecutive_failures": 0,
                    "blocked": False,
                    "last_error": None,
                }
                for lane in LANES
            },
            "bus_cursors": {},
            "report_heads": {},
            "latest_reports": {},
            "protected_baselines": {},
            "protected_policy": [],
            "missions": None,
            "external": {"cursor": None, "seen_ids": [], "errors": []},
            "events": [],
        }

    def _workspace_map(self) -> dict[str, object]:
        return {
            "version": 1,
            "run_id": self.control["run_id"],
            "source": str(self.source),
            "shared": str(self.paths.shared),
            "lanes": {
                "lane-1": {
                    "workspace": str(self.source),
                    "ownership": "original-source-and-integration",
                },
                "lane-2": {
                    "workspace": str(self.paths.workspace("lane-2")),
                    "ownership": "private-snapshot",
                },
                "lane-3": {
                    "workspace": str(self.paths.workspace("lane-3")),
                    "ownership": "private-snapshot",
                },
            },
            "artifact_roots": {
                lane: str(self.paths.artifact_root(cast("LaneName", lane)))
                for lane in LANES
            },
            "checkpoints": {
                lane: str(self.paths.checkpoint(cast("LaneName", lane)))
                for lane in LANES
            },
            "remote_actions": "not-authorized-by-this-flow",
        }

    def _plan(self, objective: str, cwd: Path | None = None) -> InitialPlan:
        prompt = planning_prompt(
            objective=objective,
            workspace_map=self._workspace_map(),
            mission_mode=self.mission_mode,
        )
        failures: list[str] = []
        for attempt in range(1, 4):
            session = self.agents.coordinator.new(cwd=cwd or self.paths.planning)
            try:
                # This block already catches backend and shape failures. Do not suppress them at
                # the session boundary, or three actionable errors collapse into an empty list.
                result = session(prompt, suppress=False, schema=InitialPlan)
            except Stopped:
                raise
            except Exception as why:  # noqa: BLE001 - retry any backend failure fresh
                failures.append(
                    f"attempt {attempt}: {type(why).__name__}: {why}"[:1000]
                )
                result = None
            finally:
                with contextlib.suppress(BaseException):
                    session.close()
            if result is not None:
                return result
            if len(failures) < attempt:
                failures.append(
                    f"attempt {attempt}: coordinator returned no structured plan"
                )
        raise RuntimeError(
            f"initial coordinator failed after 3 fresh sessions: {failures}"
        )

    def prepare(self) -> SourceLock:
        objective, resume, revised = self._resolve_objective()
        if resume:
            self.control = _json_copy(self.state)
            self._validate_resumable_control()
            self.control["objective"] = objective
            self.control["task_fingerprint"] = task_fingerprint(objective)
            self.control["updated_at"] = now()
            expected_root = (
                home() / "parallel_flame_chase" / workspace_key(self.source)
            ).resolve()
            resumed_root = Path(cast("str", self.control["run_root"])).resolve()
            if not resumed_root.is_relative_to(
                expected_root
            ) or resumed_root.name != self.control.get("run_id"):
                raise ValueError(
                    "resumable run_root is outside this workspace's runtime home"
                )
            self.paths = RunPaths(resumed_root, self.source)
            required = [
                self.paths.root,
                self.paths.shared,
                self.paths.reports,
                self.paths.private / "lane-2",
                self.paths.private / "lane-3",
                *(self.paths.reports / f"{lane}.jsonl" for lane in LANES),
            ]
            if not all(path.exists() for path in required):
                raise RuntimeError(
                    "resumable run is incomplete; refusing to recreate lost state"
                )
            validate_runtime_layout(self.paths)
            initialize_paths(self.paths, make_snapshots=False)
        else:
            self.control = self._new_control(objective)
            self.paths = RunPaths(
                Path(cast("str", self.control["run_root"])), self.source
            )
            self.paths.root.mkdir(parents=True, exist_ok=False)
            initialize_paths(self.paths, make_snapshots=True)
        self.bus = ReportBus(self.paths)
        atomic_text(self.paths.root / "objective.md", objective + "\n")
        atomic_json(self.paths.workspace_map, self._workspace_map())
        validate_runtime_layout(self.paths)
        if resume:
            self._validate_report_heads()
        else:
            self.control["report_heads"] = {
                lane: self.bus.head(cast("LaneName", lane)) for lane in LANES
            }

        if (
            not resume
            or self.control.get("plan") is None
            or (revised and not self.mission_mode)
        ):
            planning_cwd = self.paths.planning
            if resume and revised:
                planning_cwd = (
                    self.paths.shared
                    / "planning-revisions"
                    / task_fingerprint(objective)[:16]
                )
                if not planning_cwd.exists():
                    snapshot(
                        self.source,
                        planning_cwd,
                        inspect_workspace(self.source),
                    )
            plan = self._plan(objective, planning_cwd)
            self.control["plan"] = plan.model_dump(mode="json")
            if revised and not self.mission_mode:
                self.control["events"].append(
                    {
                        "at": now(),
                        "kind": "objective_replanned",
                        "task_fingerprint": task_fingerprint(objective),
                    }
                )
                for lane in LANES:
                    lane_state = self.control["lanes"][lane]
                    lane_state["blocked"] = False
                    lane_state["consecutive_failures"] = 0

        if self.mission_mode:
            self.controller = MissionController(
                cast("dict[str, Any] | None", self.control.get("missions")),
                run_id=cast("str", self.control["run_id"]),
                objective=objective,
                global_audit_hours=getattr(self.config, "global_audit_hours", 6.0),
                default_deadline_hours=getattr(
                    self.config, "mission_deadline_hours", 6.0
                ),
                default_max_turns=getattr(self.config, "max_turns_without_outcome", 6),
                clock=self.clock,
            )
            if not self.controller.data["missions"]:
                self.controller.bootstrap(
                    InitialPlan.model_validate(self.control["plan"])
                )
            elif revised:
                previous_revisions = self.controller.data.get("objective_revisions", [])
                current_hash = task_fingerprint(objective)
                if not any(
                    item.get("current_fingerprint") == current_hash
                    for item in previous_revisions
                ):
                    prior = cast("str", self.state.get("task_fingerprint", ""))
                    self.controller.revise_objective(prior, current_hash)
            self.control["missions"] = self.controller.snapshot()

        protected = tuple(getattr(self.config, "protected_paths", ()))
        baselines = cast("dict[str, object]", self.control["protected_baselines"])
        policy = list(protected)
        if self.control.get("protected_policy") != policy:
            baselines.clear()
            self.control["protected_policy"] = policy
            self.control["events"].append(
                {
                    "at": now(),
                    "kind": "protected_policy_baseline_reset",
                    "paths": policy,
                }
            )
        if protected:
            for lane in cast("tuple[LaneName, ...]", LANES):
                if lane not in baselines:
                    baselines[lane] = tree_fingerprint(
                        self.paths.workspace(lane), protected
                    )

        pairs = {
            "lane-1": (self.agents.lane_1_actor_a, self.agents.lane_1_actor_b),
            "lane-2": (self.agents.lane_2_actor_a, self.agents.lane_2_actor_b),
            "lane-3": (self.agents.lane_3_actor_a, self.agents.lane_3_actor_b),
        }
        for lane in cast("tuple[LaneName, ...]", LANES):
            lane_state = cast("dict[str, Any]", self.control["lanes"][lane])
            self.lanes[lane] = LaneRuntime(
                lane=lane,
                actors=pairs[lane],
                workspace=self.paths.workspace(lane),
                actor_at=int(lane_state.get("next_actor", 0)) % 2,
            )
        self.control["status"] = "running"
        self._persist()
        lock = SourceLock(
            home()
            / "parallel_flame_chase"
            / "locks"
            / f"{workspace_key(self.source)}.lock",
            self.source,
            cast("str", self.control["run_id"]),
        )
        return lock

    def _manifest(self) -> dict[str, object]:
        active_audit = (
            self.controller.active_audit() if self.controller is not None else None
        )
        return {
            "version": 1,
            "protocol": PROTOCOL_VERSION,
            "mode": self._mode,
            "run_id": self.control.get("run_id"),
            "status": self.control.get("status"),
            "source": str(self.source),
            "objective_fingerprint": self.control.get("task_fingerprint"),
            "updated_at": now(),
            "lanes": _json_copy(self.control.get("lanes", {})),
            "active_audit": _json_copy(active_audit),
            "integration_queue": (
                _json_copy(self.controller.data.get("integration_queue", []))
                if self.controller is not None
                else []
            ),
            "external_ingress": {
                "configured": bool(getattr(self.config, "external_events", None)),
                "cursor": _json_copy(self.control.get("external", {}).get("cursor")),
                "recent_errors": _json_copy(
                    self.control.get("external", {}).get("errors", [])[-10:]
                ),
            },
            "remote_actions": "disabled",
        }

    def _persist(self) -> None:
        validate_runtime_layout(self.paths)
        self._validate_report_heads()
        if self.controller is not None:
            self.control["missions"] = self.controller.snapshot()
        self.control["updated_at"] = now()
        self.state.clear()
        self.state.update(_json_copy(self.control))
        atomic_json(self.paths.state_mirror, self.control)
        atomic_json(self.paths.manifest, self._manifest())

    def _validate_report_heads(self) -> None:
        held = self.control.get("report_heads")
        if not isinstance(held, dict) or set(held) != set(LANES):
            raise RuntimeError("authoritative report-log heads are missing")
        for lane in cast("tuple[LaneName, ...]", LANES):
            if held.get(lane) != self.bus.head(lane):
                raise RuntimeError(f"{lane} report log changed outside the runtime")

    def _initial_brief(self, lane: LaneName) -> dict[str, object]:
        plan = InitialPlan.model_validate(self.control["plan"])
        brief = next(item for item in plan.lanes if item.lane == lane)
        return brief.model_dump(mode="json")

    def _identity(self, lane: LaneName) -> dict[str, object]:
        if self.controller is not None:
            return self.controller.identity(lane)
        turns = int(self.control["lanes"][lane].get("turns", 0))
        return {
            "version": 1,
            "run_id": self.control["run_id"],
            "lane": lane,
            "mission_id": None,
            "generation": turns,
        }

    def _active_integration_item(self) -> dict[str, object] | None:
        if self.controller is None:
            return None
        mission = self.controller.current_mission("lane-1")
        if cast("dict[str, Any]", mission["spec"]).get("kind") != "integration":
            return None
        for item in cast(
            "list[dict[str, object]]", self.controller.data["integration_queue"]
        ):
            if item.get("integration_mission_id") == mission["id"]:
                return _json_copy(item)
        return None

    def _activate_queued_integration(self) -> bool:
        """At Lane 1's natural boundary, validate and activate the next handoff."""
        if self.controller is None or self.controller.active_audit() is not None:
            return False
        current = self.controller.current_mission("lane-1")
        if cast("dict[str, Any]", current["spec"]).get("kind") == "integration":
            return False
        item = self.controller.queued_integration()
        if item is None:
            return False
        source_lane = item.get("source_lane")
        artifacts = item.get("artifacts")
        if source_lane not in LANES or not isinstance(artifacts, list):
            self.controller.invalidate_integration(
                cast("str", item["id"]), "malformed package"
            )
            self._persist()
            return True
        root = self.paths.artifact_root(cast("LaneName", source_lane))
        if not artifacts_still_match(root, cast("list[dict[str, object]]", artifacts)):
            self.controller.invalidate_integration(
                cast("str", item["id"]), "artifact package changed after acceptance"
            )
            self._persist()
            return True
        self.controller.activate_integration(cast("str", item["id"]))
        self._persist()
        return True

    def _schedule_lane(self, runtime: LaneRuntime) -> None:
        lane = runtime.lane
        durable = cast("dict[str, Any]", self.control["lanes"][lane])
        if runtime.future is not None or durable.get("blocked"):
            return
        if self.controller is not None:
            if self.controller.auditing(lane):
                return
            mission = self.controller.current_mission(lane)
            if mission.get("status") != "active":
                return
            if lane == "lane-1":
                self._activate_queued_integration()
                if self.controller.auditing(lane):
                    return
                mission = self.controller.current_mission(lane)
            mission_document: dict[str, object] | None = _json_copy(mission)
        else:
            mission_document = None
        validate_runtime_layout(self.paths)
        self._validate_report_heads()
        cursors = cast("dict[str, Any]", self.control["bus_cursors"])
        unread, acknowledgements = self.bus.unread(lane, cursors)
        identity = self._identity(lane)
        turn = int(durable.get("turns", 0)) + 1
        actor_index = int(durable.get("next_actor", runtime.actor_at)) % 2
        runtime.actor_at = actor_index
        actor = runtime.actors[actor_index]
        role = f"{lane}-actor-{'a' if actor_index == 0 else 'b'}"
        prompt = lane_prompt(
            objective=cast("str", self.control["objective"]),
            lane=lane,
            actor_role=role,
            turn=turn,
            workspace_map=self._workspace_map(),
            mission=mission_document,
            initial_brief=self._initial_brief(lane),
            unread_reports=unread,
            checkpoint_path=str(self.paths.checkpoint(lane)),
            artifact_root=str(self.paths.artifact_root(lane)),
            identity=identity,
            integration_item=self._active_integration_item()
            if lane == "lane-1"
            else None,
            runtime_status={
                "consecutive_failures": int(durable.get("consecutive_failures", 0)),
                "last_error": durable.get("last_error"),
            },
        )
        runtime.identity = identity
        runtime.pending_ack = acknowledgements
        runtime.checkpoint_before = _checkpoint_fingerprint(self.paths.checkpoint(lane))
        runtime.interjected_at = None
        runtime.closed_for_audit = False
        runtime.quiesced_revision = None
        try:
            session = actor.new(cwd=runtime.workspace)
            runtime.session = session
            runtime.future = self.executor.submit(
                session,
                prompt,
                suppress=True,
                schema=LaneReport,
            )
        except Stopped:
            raise
        except Exception as why:  # noqa: BLE001 - isolate arbitrary actor startup failures
            if runtime.session is not None:
                with contextlib.suppress(BaseException):
                    runtime.session.close()
            runtime.session = None
            self._record_failure(runtime, f"actor session could not start: {why}")
            self._persist()

    def _protected_violation(self, lane: LaneName) -> str | None:
        protected = tuple(getattr(self.config, "protected_paths", ()))
        if not protected:
            return None
        before = self.control["protected_baselines"].get(lane)
        after = tree_fingerprint(self.paths.workspace(lane), protected)
        if before == after:
            return None
        return "configured protected paths changed; the runtime blocked the lane without rollback"

    def _record_report(
        self,
        runtime: LaneRuntime,
        report: LaneReport,
        *,
        recovered: bool,
    ) -> dict[str, object]:
        lane = runtime.lane
        validate_runtime_layout(self.paths)
        self._validate_report_heads()
        durable = cast("dict[str, Any]", self.control["lanes"][lane])
        artifacts: list[dict[str, object]] = []
        if report.deliverable is not None:
            artifacts = validate_deliverable(
                self.paths.artifact_root(lane), report.deliverable
            )
        record = {
            "version": 1,
            "report_id": uuid.uuid4().hex,
            "at": now(),
            "run_id": self.control["run_id"],
            "lane": lane,
            "actor": "a" if runtime.actor_at == 0 else "b",
            "turn": int(durable.get("turns", 0)) + 1,
            "mission_id": runtime.identity.get("mission_id"),
            "generation": runtime.identity.get("generation"),
            "recovered_from_checkpoint": recovered,
            **report.model_dump(mode="json"),
            "artifacts": artifacts,
        }
        self.control["report_heads"][lane] = self.bus.publish(
            lane, cast("dict[str, object]", record)
        )
        self.control["latest_reports"][lane] = _json_copy(record)
        ReportBus.acknowledge(
            lane,
            cast("dict[str, Any]", self.control["bus_cursors"]),
            runtime.pending_ack,
        )
        durable["turns"] = int(durable.get("turns", 0)) + 1
        durable["next_actor"] = 1 - runtime.actor_at
        durable["consecutive_failures"] = 0
        durable["last_error"] = None
        if self.controller is not None:
            self.controller.observe(lane, cast("dict[str, Any]", record))
        elif report.status == "blocked":
            durable["blocked"] = True
        self.completed_turns += 1
        return record

    def _record_failure(self, runtime: LaneRuntime, error: str) -> None:
        lane = runtime.lane
        durable = cast("dict[str, Any]", self.control["lanes"][lane])
        failure: dict[str, object] = {
            "version": 1,
            "report_id": uuid.uuid4().hex,
            "at": now(),
            "run_id": self.control["run_id"],
            "lane": lane,
            "actor": "a" if runtime.actor_at == 0 else "b",
            "turn": int(durable.get("turns", 0)) + 1,
            "mission_id": runtime.identity.get("mission_id"),
            "generation": runtime.identity.get("generation"),
            "recovered_from_checkpoint": False,
            "status": "turn_failed",
            "summary": error[:2000],
            "changes": [],
            "evidence": [],
            "tests": [],
            "risks": [],
            "next_step": "Retry with the alternating partner or request a scoped audit.",
            "deliverable": None,
            "artifacts": [],
        }
        self._validate_report_heads()
        self.control["report_heads"][lane] = self.bus.publish(lane, failure)
        self.control["latest_reports"][lane] = _json_copy(failure)
        durable["next_actor"] = 1 - runtime.actor_at
        durable["consecutive_failures"] = (
            int(durable.get("consecutive_failures", 0)) + 1
        )
        durable["last_error"] = error[:2000]
        self.control["events"].append(
            {
                "at": now(),
                "kind": "turn_failed",
                "lane": lane,
                "actor": "a" if runtime.actor_at == 0 else "b",
                "mission_id": runtime.identity.get("mission_id"),
                "generation": runtime.identity.get("generation"),
                "error": error[:2000],
            }
        )
        if durable["consecutive_failures"] < 2:
            return
        durable["blocked"] = self.controller is None
        if self.controller is not None:
            self.controller.trigger(
                "actor_pair_blocked",
                {
                    "event_id": (
                        f"{runtime.identity.get('mission_id')}:actor-pair:"
                        f"{runtime.identity.get('generation')}"
                    ),
                    "at": now(),
                    "lane": lane,
                    "error": error[:2000],
                },
                scope="targeted",
                targets=[lane],
            )

    def _collect_lane(self, runtime: LaneRuntime) -> None:
        future = runtime.future
        if future is None or not future.done():
            return
        runtime.future = None
        result: LaneReport | None = None
        recovered = False
        error: str | None = None
        try:
            result = future.result()
        except Stopped:
            if not runtime.closed_for_audit:
                raise
            error = "session closed by scoped audit"
        except Exception as why:  # noqa: BLE001 - isolate arbitrary actor backend failures
            error = f"{type(why).__name__}: {why}"[:2000]
        if result is None:
            result = _checkpoint_report(
                self.paths.checkpoint(runtime.lane),
                runtime.checkpoint_before,
                runtime.identity,
            )
            recovered = result is not None
        current_identity = self._identity(runtime.lane)
        stale = any(
            runtime.identity.get(key) != current_identity.get(key)
            for key in ("run_id", "lane", "mission_id", "generation")
        )
        violation = self._protected_violation(runtime.lane)
        if stale:
            self.control["events"].append(
                {
                    "at": now(),
                    "kind": "stale_turn_discarded",
                    "lane": runtime.lane,
                    "identity": runtime.identity,
                }
            )
        elif violation is not None:
            self._record_failure(runtime, violation)
            durable = self.control["lanes"][runtime.lane]
            durable["consecutive_failures"] = max(2, durable["consecutive_failures"])
            if self.controller is not None:
                self.controller.trigger(
                    "protected_path_violation",
                    {
                        "event_id": (
                            f"{runtime.identity.get('mission_id')}:protected:"
                            f"{runtime.identity.get('generation')}"
                        ),
                        "at": now(),
                        "lane": runtime.lane,
                        "reason": violation,
                    },
                    scope="targeted",
                    targets=[runtime.lane],
                )
            else:
                durable["blocked"] = True
        elif result is not None and result.status != "turn_failed":
            try:
                self._record_report(runtime, result, recovered=recovered)
            except (OSError, ValueError) as why:
                self._record_failure(runtime, f"invalid deliverable/report: {why}")
        else:
            self._record_failure(
                runtime,
                error
                or (result.summary if result is not None else None)
                or "actor returned no structured report",
            )
        if runtime.session is not None:
            with contextlib.suppress(BaseException):
                runtime.session.close()
        runtime.session = None
        runtime.pending_ack = {}
        self._persist()

    def _checkpoint_evidence(self, lane: LaneName) -> dict[str, object] | None:
        path = self.paths.checkpoint(lane)
        try:
            if path.stat().st_size > CHECKPOINT_FILE_LIMIT:
                return None
            checkpoint = LaneCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        expected = self._identity(lane)
        identity = checkpoint.identity.model_dump(mode="json")
        if any(
            identity.get(key) != expected.get(key)
            for key in ("run_id", "lane", "mission_id", "generation")
        ):
            return None
        return checkpoint.model_dump(mode="json")

    def _quiesce_targets(self) -> None:
        if self.controller is None:
            return
        audit = self.controller.active_audit()
        if audit is None:
            return
        revision = int(audit["revision"])
        grace = float(getattr(self.config, "interrupt_grace_seconds", 60.0))
        for lane in self.controller.targets():
            runtime = self.lanes[lane]
            if runtime.quiesced_revision == revision:
                continue
            future = runtime.future
            interruption: dict[str, object]
            if future is not None and not future.done():
                if runtime.interjected_at is None:
                    try:
                        cast("Session", runtime.session).interject(
                            "A scoped audit is starting. Stop at a safe boundary and write your "
                            "exact-identity checkpoint now; do not begin new work."
                        )
                        interruption = {"method": "interject", "at": now()}
                    except Exception as why:  # noqa: BLE001 - interjection is best-effort
                        interruption = {
                            "method": "interject-unavailable",
                            "at": now(),
                            "error": f"{type(why).__name__}: {why}"[:1000],
                        }
                    runtime.interjected_at = time.monotonic()
                    self.control["events"].append(
                        {"kind": "audit_interjection", "lane": lane, **interruption}
                    )
                    continue
                if time.monotonic() - runtime.interjected_at < grace:
                    continue
                if not runtime.closed_for_audit:
                    runtime.closed_for_audit = True
                    with contextlib.suppress(BaseException):
                        cast("Session", runtime.session).close()
                    interruption = {"method": "session-close-after-grace", "at": now()}
                else:
                    interruption = {"method": "session-already-closed", "at": now()}
            else:
                interruption = {
                    "method": "natural-boundary"
                    if future is None
                    else "turn-completed",
                    "at": now(),
                }
            evidence = {
                "latest_report": _json_copy(self.control["latest_reports"].get(lane)),
                "checkpoint": self._checkpoint_evidence(lane),
                "runtime_identity": _json_copy(runtime.identity),
                "interruption": interruption,
            }
            self.controller.mark_quiesced(lane, interruption, evidence)
            runtime.quiesced_revision = revision
            self._persist()

    def _audit_cwd_and_packet(self) -> tuple[Path, dict[str, object]]:
        validate_runtime_layout(self.paths)
        self._validate_report_heads()
        controller = cast("MissionController", self.controller)
        audit = cast("dict[str, Any]", controller.active_audit())
        directory = self.paths.audits / cast("str", audit["id"])
        directory.mkdir(parents=True, exist_ok=True)
        packet = controller.decision_packet(
            cast("dict[str, object]", self.control["latest_reports"]),
            self._manifest(),
        )
        revision = int(audit["revision"])
        packet_path = directory / f"packet-r{revision}.json"
        atomic_json(packet_path, packet)
        if audit["scope"] != "global":
            return directory, packet
        source_snapshot = directory / f"source-r{revision}"
        if not source_snapshot.exists():
            size = inspect_workspace(self.source)
            snapshot(self.source, source_snapshot, size)
        return source_snapshot, packet

    def _start_audit_decision(self) -> None:
        if self.controller is None or self.coordinator.future is not None:
            return
        if not self.controller.ready_for_decision():
            return
        if time.monotonic() < self.coordinator.retry_after:
            return
        deciding = self.controller.deciding()
        if deciding is None:
            return
        audit_id, revision = deciding
        cwd, packet = self._audit_cwd_and_packet()
        self.coordinator.audit_id = audit_id
        self.coordinator.revision = revision
        self.coordinator.closed_for_stale_revision = False
        try:
            session = self.agents.coordinator.new(cwd=cwd)
            self.coordinator.session = session
            self.coordinator.future = self.executor.submit(
                _run_audit_session, session, packet
            )
        except Stopped:
            raise
        except Exception as why:  # noqa: BLE001 - retry arbitrary coordinator startup failures
            if self.coordinator.session is not None:
                with contextlib.suppress(BaseException):
                    self.coordinator.session.close()
            self.coordinator.session = None
            self.controller.record_attempt(
                {
                    "at": now(),
                    "audit_id": audit_id,
                    "revision": revision,
                    "error": f"coordinator session could not start: {why}"[:2000],
                }
            )
            delays = (5.0, 15.0, 60.0, 300.0)
            index = min(self.coordinator.retry_count, len(delays) - 1)
            self.coordinator.retry_count += 1
            self.coordinator.retry_after = time.monotonic() + delays[index]
        self._persist()

    def _accepted_artifacts_error(self, decision: AuditDecision) -> str | None:
        if self.controller is None:
            return None
        for item in decision.lanes:
            if item.verdict != "accept":
                continue
            mission = self.controller.current_mission(item.lane)
            outcome = mission.get("outcome")
            artifacts = outcome.get("artifacts") if isinstance(outcome, dict) else None
            if not isinstance(artifacts, list) or not artifacts_still_match(
                self.paths.artifact_root(item.lane),
                cast("list[dict[str, object]]", artifacts),
            ):
                return f"{item.lane} artifact package changed after publication"
        return None

    def _collect_audit_decision(self) -> None:
        future = self.coordinator.future
        if future is None or not future.done():
            return
        self.coordinator.future = None
        session = self.coordinator.session
        self.coordinator.session = None
        try:
            decision, attempts = future.result()
        except Stopped:
            if not self.coordinator.closed_for_stale_revision:
                raise
            decision = None
            attempts = [
                {
                    "at": now(),
                    "error": "stale coordinator session was closed by the runtime",
                }
            ]
        except Exception as why:  # noqa: BLE001 - retry arbitrary coordinator failures
            decision = None
            attempts: list[dict[str, object]] = [
                {"at": now(), "error": f"{type(why).__name__}: {why}"[:2000]}
            ]
        if session is not None:
            with contextlib.suppress(BaseException):
                session.close()
        controller = cast("MissionController", self.controller)
        for attempt in attempts:
            controller.record_attempt(attempt)
        active = controller.active_audit()
        stale_revision = active is None or (
            self.coordinator.audit_id != active["id"]
            or self.coordinator.revision != active["revision"]
        )
        if stale_revision:
            controller.record_attempt(
                {
                    "at": now(),
                    "valid": False,
                    "audit_id": self.coordinator.audit_id,
                    "revision": self.coordinator.revision,
                    "error": "decision discarded because a newer audit revision is active",
                }
            )
            self.coordinator.retry_count = 0
            self.coordinator.retry_after = 0.0
            self.coordinator.closed_for_stale_revision = False
            self._persist()
            return
        error = "coordinator exhausted proposal and repair attempts"
        if decision is not None:
            error = controller.validate_decision(
                decision
            ) or self._accepted_artifacts_error(decision)
            if error is None:
                resumed = controller.apply(decision)
                for lane in resumed:
                    runtime = self.lanes[lane]
                    runtime.quiesced_revision = None
                    if runtime.future is None:
                        runtime.interjected_at = None
                        runtime.closed_for_audit = False
                    durable = self.control["lanes"][lane]
                    durable["consecutive_failures"] = 0
                    durable["blocked"] = False
                self.coordinator.retry_count = 0
                self.coordinator.retry_after = 0.0
                self.coordinator.closed_for_stale_revision = False
                self._persist()
                return
        controller.record_attempt(
            {
                "at": now(),
                "valid": False,
                "audit_id": self.coordinator.audit_id,
                "revision": self.coordinator.revision,
                "error": error,
            }
        )
        delays = (5.0, 15.0, 60.0, 300.0)
        index = min(self.coordinator.retry_count, len(delays) - 1)
        self.coordinator.retry_count += 1
        self.coordinator.retry_after = time.monotonic() + delays[index]
        self.coordinator.closed_for_stale_revision = False
        self._persist()

    def _supersede_stale_coordinator(self) -> None:
        if self.controller is None or self.coordinator.future is None:
            return
        audit = self.controller.active_audit()
        if audit is None:
            return
        if (
            self.coordinator.audit_id == audit["id"]
            and self.coordinator.revision == audit["revision"]
        ):
            return
        if self.coordinator.closed_for_stale_revision:
            return
        if self.coordinator.session is not None:
            with contextlib.suppress(BaseException):
                self.coordinator.session.close()
        self.coordinator.closed_for_stale_revision = True

    def _read_external_events(self) -> None:
        if self.controller is None:
            return
        configured = getattr(self.config, "external_events", None)
        reader = ExternalEventReader(Path(configured).resolve() if configured else None)
        held = cast("dict[str, Any]", self.control["external"])
        events, errors, cursor, seen = reader.read(
            cast("str", self.control["run_id"]),
            cast("dict[str, object] | None", held.get("cursor")),
            cast("list[str]", held.get("seen_ids", [])),
        )
        held["cursor"] = cursor
        held["seen_ids"] = seen
        held["errors"] = [
            *cast("list[object]", held.get("errors", []))[-100:],
            *errors,
        ][-200:]
        for event in events:
            self.controller.observe_external(event)
        if events or errors:
            self._persist()

    def _mission_cycle(self) -> None:
        controller = cast("MissionController", self.controller)
        self._read_external_events()
        controller.tick()
        self._supersede_stale_coordinator()
        self._quiesce_targets()
        self._collect_audit_decision()
        self._start_audit_decision()

    def _close_sessions(self) -> None:
        for runtime in self.lanes.values():
            if runtime.session is not None:
                with contextlib.suppress(BaseException):
                    runtime.session.close()
        if self.coordinator.session is not None:
            with contextlib.suppress(BaseException):
                self.coordinator.session.close()

    def run(self) -> None:
        prepared = False
        try:
            lock = self.prepare()
            prepared = True
            print(
                f"parallel_flame_chase:{self._mode} · run {self.control['run_id']} · "
                f"state {self.paths.root}"
            )
            with lock:
                while True:
                    for runtime in self.lanes.values():
                        self._collect_lane(runtime)
                    if self.controller is not None:
                        self._mission_cycle()
                    if (
                        self.max_turns is not None
                        and self.completed_turns >= self.max_turns
                    ):
                        self.control["status"] = "test-complete"
                        self._persist()
                        return
                    for runtime in self.lanes.values():
                        self._schedule_lane(runtime)
                    self.sleeper(float(getattr(self.config, "rest_seconds", 1.0)))
        except (Stopped, KeyboardInterrupt):
            self._close_sessions()
            if prepared:
                self.control["status"] = "stopped"
                self._persist()
            raise
        except BaseException:
            self._close_sessions()
            if prepared:
                self.control["status"] = "failed"
                self._persist()
            raise
        finally:
            self._close_sessions()
            self.executor.shutdown(wait=False, cancel_futures=True)


def execute(
    agents: Any,
    task: str,
    config: Any,
    state: dict[str, Any] | None,
    *,
    mission_mode: bool,
    _clock: Callable[[], dt.datetime] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    _max_turns: int | None = None,
) -> None:
    """Run the shared engine; underscored controls exist for deterministic tests only."""
    ParallelRuntime(
        agents,
        task,
        config,
        state,
        mission_mode=mission_mode,
        clock=_clock,
        sleeper=_sleep,
        max_turns=_max_turns,
    ).run()


__all__ = ["ParallelRuntime", "execute"]
