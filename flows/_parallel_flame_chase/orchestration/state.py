"""Run creation, resume validation, planning, and durable state persistence."""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from hmz.flows import Stopped, home

from ..core.models import LANES, InitialPlan, LaneName
from ..core.utils import (
    atomic_json,
    atomic_text,
    close_safely,
    json_copy,
    now,
    task_fingerprint,
    workspace_key,
)
from ..lanes.prompts import planning_prompt
from ..lanes.runtime import LaneRuntime
from ..persistence.events import ReportBus
from ..persistence.leaderboard import empty_leaderboard, validate_leaderboard
from ..persistence.workspace import (
    RunPaths,
    SourceLock,
    initialize_paths,
    inspect_workspace,
    snapshot,
    validate_runtime_layout,
)

STATE_VERSION = 1
PROTOCOL_VERSION = 1
CONTINUATION_MARKERS = {
    "continue",
    "continue.",
    "resume",
    "resume.",
    "go on",
    "继续",
    "继续。",
}


class RuntimeState:
    """Own run configuration and every durable single-writer control record."""

    mode_name = "base"
    skill_name = "parallel-flame-chase"
    planning_cadence = (
        "This is the only coordinator turn; lanes will subsequently self-coordinate "
        "through durable reports."
    )
    orchestrator_role_name = "coordinator"
    replan_on_objective_revision = True

    def __init__(
        self,
        agents: Any,
        task: str,
        config: Any,
        state: dict[str, Any] | None,
        *,
        clock: Callable[[], dt.datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_turns: int | None = None,
    ) -> None:
        self.agents = agents
        self.raw_task = task
        self.config = config
        self.state = state if state is not None else {}
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.sleeper = sleeper
        self.max_turns = max_turns
        self.source = Path.cwd().resolve()
        self.control: dict[str, Any] = {}
        self.paths: RunPaths
        self.bus: ReportBus
        self.lanes: dict[LaneName, LaneRuntime] = {}
        self.executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="parallel-flame",
        )
        self.completed_turns = 0

    @property
    def _mode(self) -> str:
        return self.mode_name

    def _new_mode_control(self) -> dict[str, object]:
        """Return durable fields owned by a specialized mode."""
        return {}

    def _validate_mode_control(self) -> None:
        """Validate specialized fields after the shared resume contract."""

    def _prepare_mode(self, objective: str, *, revised: bool) -> None:
        """Attach mode-specific state before workers are scheduled."""

    def _before_persist(self) -> None:
        """Snapshot mode-specific state into the shared control document."""

    def _manifest_fields(self) -> dict[str, object]:
        """Project mode-specific observability fields into the manifest."""
        return {}

    def _initialize_mode_paths(self) -> None:
        """Create runtime paths owned by a specialized mode."""

    def _validate_mode_layout(self) -> None:
        """Validate runtime paths owned by a specialized mode."""

    def _validate_layout(self) -> None:
        validate_runtime_layout(self.paths)
        self._validate_mode_layout()

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

    def _validate_resumable_control(self) -> None:
        """Reject partial or cross-protocol state instead of guessing missing facts."""
        if self.control.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("unsupported parallel Flame Chase state protocol")
        if self.control.get("mode") != self._mode:
            raise ValueError("resumable state belongs to another flow mode")
        run_id = self.control.get("run_id")
        if not isinstance(run_id, str) or not run_id:
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
        for name in ("bus_cursors", "latest_reports"):
            if not isinstance(self.control.get(name), dict):
                raise TypeError(f"resumable state field {name!r} is malformed")
        if not isinstance(self.control.get("events"), list):
            raise TypeError("resumable state events are malformed")
        validate_leaderboard(
            self.control.get("candidate_board"), cast("str", self.control["run_id"])
        )
        self._validate_mode_control()

    def _new_control(self, objective: str) -> dict[str, Any]:
        stamp = self.clock().astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{uuid.uuid4().hex[:10]}"
        root = home() / "parallel_flame_chase" / workspace_key(self.source) / run_id
        control: dict[str, Any] = {
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
            "latest_reports": {},
            "candidate_board": empty_leaderboard(run_id),
            "events": [],
        }
        control.update(self._new_mode_control())
        return control

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
                lane: str(self.paths.artifact_root(lane)) for lane in LANES
            },
            "checkpoints": {lane: str(self.paths.checkpoint(lane)) for lane in LANES},
            "candidate_submissions": {
                "all_lanes_may_submit": True,
                "local_evaluator_only": True,
                "report_field": "submission",
                "requires_reconstructable_deliverable": True,
                "leaderboard": str(self.paths.leaderboard),
                "current": json_copy(self.control["candidate_board"]),
            },
            "remote_actions": "not-authorized-by-this-flow",
        }

    def _plan(self, objective: str, cwd: Path | None = None) -> InitialPlan:
        prompt = planning_prompt(
            objective=objective,
            workspace_map=self._workspace_map(),
            skill=self.skill_name,
            role_name=self.orchestrator_role_name,
            cadence=self.planning_cadence,
        )
        failures: list[str] = []
        for attempt in range(1, 4):
            session = self.agents.coordinator.new(cwd=cwd or self.paths.planning)
            try:
                result = session(prompt, suppress=False, schema=InitialPlan)
            except Stopped:
                raise
            except Exception as why:  # noqa: BLE001 - retry any backend failure fresh
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
                    f"attempt {attempt}: coordinator returned no structured plan"
                )
        raise RuntimeError(
            f"initial coordinator failed after 3 fresh sessions: {failures}"
        )

    def _resume_run(self, objective: str) -> None:
        """Load one complete compatible run without recreating missing durable state."""
        self.control = json_copy(self.state)
        if "candidate_board" not in self.control:
            run_id = self.control.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("resumable state has no run_id")
            self.control["candidate_board"] = empty_leaderboard(run_id)
        self._validate_resumable_control()
        self.control.update(
            objective=objective,
            task_fingerprint=task_fingerprint(objective),
            updated_at=now(),
        )
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
        required = (
            self.paths.root,
            self.paths.shared,
            self.paths.reports,
            self.paths.private / "lane-2",
            self.paths.private / "lane-3",
            *(self.paths.reports / f"{lane}.jsonl" for lane in LANES),
        )
        if not all(path.exists() for path in required):
            raise RuntimeError(
                "resumable run is incomplete; refusing to recreate lost state"
            )
        self._validate_layout()
        initialize_paths(self.paths, make_snapshots=False)

    def _create_run(self, objective: str) -> None:
        """Create durable directories and private snapshots for a fresh run."""
        self.control = self._new_control(objective)
        self.paths = RunPaths(Path(cast("str", self.control["run_root"])), self.source)
        self.paths.root.mkdir(parents=True, exist_ok=False)
        initialize_paths(self.paths, make_snapshots=True)

    def _open_run(self, objective: str, resume: bool) -> None:
        if resume:
            self._resume_run(objective)
        else:
            self._create_run(objective)
        self._initialize_mode_paths()
        self.bus = ReportBus(self.paths)
        atomic_text(self.paths.root / "objective.md", objective + "\n")
        atomic_json(self.paths.workspace_map, self._workspace_map())
        self._validate_layout()

    def _planning_workspace(self, objective: str) -> Path:
        """Snapshot revised source so planning cannot mutate Lane 1."""
        workspace = (
            self.paths.shared / "planning-revisions" / task_fingerprint(objective)[:16]
        )
        if not workspace.exists():
            snapshot(self.source, workspace, inspect_workspace(self.source))
        return workspace

    def _prepare_plan(self, objective: str, *, resume: bool, revised: bool) -> None:
        replan = resume and revised and self.replan_on_objective_revision
        needs_plan = not resume or self.control.get("plan") is None or replan
        if not needs_plan:
            return
        planning_cwd = (
            self._planning_workspace(objective)
            if resume and revised
            else self.paths.planning
        )
        plan = self._plan(objective, planning_cwd)
        self.control["plan"] = plan.model_dump(mode="json")
        if not replan:
            return
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

    def _prepare_lanes(self) -> None:
        """Attach ephemeral actor handles to each durable lane record."""
        pairs = {
            "lane-1": (self.agents.lane_1_actor_a, self.agents.lane_1_actor_b),
            "lane-2": (self.agents.lane_2_actor_a, self.agents.lane_2_actor_b),
            "lane-3": (self.agents.lane_3_actor_a, self.agents.lane_3_actor_b),
        }
        for lane in LANES:
            lane_state = cast("dict[str, Any]", self.control["lanes"][lane])
            self.lanes[lane] = self._make_lane_runtime(
                lane=lane,
                actors=pairs[lane],
                workspace=self.paths.workspace(lane),
                actor_at=int(lane_state.get("next_actor", 0)) % 2,
            )

    def _make_lane_runtime(self, **fields: Any) -> LaneRuntime:
        """Build an ephemeral lane handle, overridable by specialized modes."""
        return LaneRuntime(**fields)

    def _source_lock(self) -> SourceLock:
        return SourceLock(
            home()
            / "parallel_flame_chase"
            / "locks"
            / f"{workspace_key(self.source)}.lock",
            self.source,
            cast("str", self.control["run_id"]),
        )

    def prepare(self) -> SourceLock:
        """Resolve, open, plan, and attach one resumable parallel run."""
        objective, resume, revised = self._resolve_objective()
        self._open_run(objective, resume)
        self._prepare_plan(objective, resume=resume, revised=revised)
        self._prepare_mode(objective, revised=revised)
        self._prepare_lanes()
        self.control["status"] = "running"
        self._persist()
        return self._source_lock()

    def _manifest(self) -> dict[str, object]:
        manifest: dict[str, object] = {
            "version": 1,
            "protocol": PROTOCOL_VERSION,
            "mode": self._mode,
            "run_id": self.control.get("run_id"),
            "status": self.control.get("status"),
            "source": str(self.source),
            "objective_fingerprint": self.control.get("task_fingerprint"),
            "updated_at": now(),
            "lanes": json_copy(self.control.get("lanes", {})),
            "candidate_board": json_copy(self.control.get("candidate_board", {})),
            "remote_actions": "disabled",
        }
        manifest.update(self._manifest_fields())
        return manifest

    def _persist(self) -> None:
        self._validate_layout()
        self._before_persist()
        self.control["updated_at"] = now()
        self.state.clear()
        self.state.update(json_copy(self.control))
        atomic_json(self.paths.state_mirror, self.control)
        atomic_json(self.paths.manifest, self._manifest())
        atomic_json(self.paths.leaderboard, self.control["candidate_board"])
