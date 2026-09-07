"""Run creation, resume validation, planning, and durable state persistence."""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from hmz.flows import Question, Stopped, home

from ..core.api import (
    DEFAULT_WORKSPACE_COPY_WARNING_THRESHOLD_BYTES,
    DEFAULT_WORKSPACE_FILE_WARNING_THRESHOLD,
)
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
    WorkspaceStats,
    initialize_paths,
    inspect_workspace_stats,
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
_CONFIRMATION_OPTIONS = ("Start anyway", "Stop")
_ACCEPTED_CONFIRMATIONS = frozenset(
    {"a", "1", "y", "yes", "是", "继续", "start anyway", "proceed"}
)


class WorkspaceStartupCancelled(Stopped):
    """A large-workspace confirmation ended startup before new copies were made."""


def _confirmed(answer: str | None) -> bool:
    """Return whether the person selected the option to continue startup."""
    if not isinstance(answer, str):
        return False
    normalized = answer.strip().casefold()
    if normalized in _ACCEPTED_CONFIRMATIONS:
        return True
    return normalized.startswith("a. start anyway")


def _positive_int(value: object, fallback: int) -> int:
    """Return a validated runtime integer when tests or older callers bypass config."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return fallback
    return value


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
    executor_workers = 4
    lane_names: tuple[LaneName, ...] = LANES

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
        self._workspace_stats: WorkspaceStats | None = None
        self.executor = ThreadPoolExecutor(
            max_workers=self.executor_workers,
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
        validate_runtime_layout(self.paths, self.lane_names)
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
        self._validate_plan(self.control.get("plan"))
        lanes = self.control.get("lanes")
        if not isinstance(lanes, dict) or set(lanes) != set(self.lane_names):
            raise ValueError("resumable state has the wrong lane topology")
        for lane in self.lane_names:
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
                for lane in self.lane_names
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
                lane: str(self.paths.artifact_root(lane)) for lane in self.lane_names
            },
            "checkpoints": {
                lane: str(self.paths.checkpoint(lane)) for lane in self.lane_names
            },
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

    def _validate_plan(self, value: object) -> Any:
        """Validate the mode's durable planning document."""
        return InitialPlan.model_validate(value)

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
            *(
                self.paths.private / lane
                for lane in self.lane_names
                if lane != "lane-1"
            ),
            *(self.paths.reports / f"{lane}.jsonl" for lane in self.lane_names),
        )
        if not all(path.exists() for path in required):
            raise RuntimeError(
                "resumable run is incomplete; refusing to recreate lost state"
            )
        self._validate_layout()
        initialize_paths(self.paths, make_snapshots=False, lanes=self.lane_names)

    def _create_run(self, objective: str) -> None:
        """Create durable directories and private snapshots for a fresh run."""
        self.control = self._new_control(objective)
        self.paths = RunPaths(Path(cast("str", self.control["run_root"])), self.source)
        self.paths.root.mkdir(parents=True, exist_ok=False)
        initialize_paths(
            self.paths,
            make_snapshots=True,
            lanes=self.lane_names,
            source_size=self._workspace_statistics().total_bytes,
        )

    def _workspace_copy_plan(self, *, resume: bool, revised: bool) -> tuple[int, str]:
        """Describe source-sized workspace materializations needed by this start."""
        if not resume:
            private_lanes = tuple(lane for lane in self.lane_names if lane != "lane-1")
            copies = 1 + len(private_lanes)
            destinations = ", ".join(("planning", *private_lanes))
            return copies, f"{copies} workspace snapshots ({destinations})"
        if revised and self.replan_on_objective_revision:
            return 1, "1 revised-objective planning snapshot"
        return 0, ""

    def _workspace_statistics(self) -> WorkspaceStats:
        """Inspect the source once and reuse the result during this startup."""
        if self._workspace_stats is None:
            self._workspace_stats = inspect_workspace_stats(self.source)
        return self._workspace_stats

    def _confirm_workspace_copies(self, *, copies: int, description: str) -> None:
        """Warn for a large copy plan and optionally require a person's confirmation."""
        if copies < 1:
            return
        stats = self._workspace_statistics()
        file_threshold = _positive_int(
            getattr(self.config, "workspace_file_warning_threshold", None),
            DEFAULT_WORKSPACE_FILE_WARNING_THRESHOLD,
        )
        byte_threshold = _positive_int(
            getattr(self.config, "workspace_copy_warning_threshold_bytes", None),
            DEFAULT_WORKSPACE_COPY_WARNING_THRESHOLD_BYTES,
        )
        estimated_bytes = stats.total_bytes * copies
        if stats.regular_files <= file_threshold and estimated_bytes <= byte_threshold:
            return
        warning = (
            f"WARNING: source workspace {self.source} contains "
            f"{stats.regular_files:,} regular files and {stats.total_bytes:,} apparent bytes.\n"
            f"Starting Parallel Flame Chase will create {description}; the rough source-sized "
            f"materialization estimate is {estimated_bytes:,} bytes before filesystem or Git "
            "optimizations.\n"
            f"Warning thresholds: {file_threshold:,} files or {byte_threshold:,} estimated "
            "bytes. No new workspace copy has been created yet."
        )
        print(warning)
        if getattr(self.config, "confirm_large_workspace_copies", False) is not True:
            print("Interactive confirmation is disabled; continuing startup.")
            return
        human = getattr(self.agents, "human", None)
        asked = cast(
            "Callable[[Question], str | None] | None",
            getattr(human, "asked", None),
        )
        if not callable(asked):
            print("No interactive confirmation is available; startup cancelled.")
            raise WorkspaceStartupCancelled(
                "large workspace startup requires confirmation"
            )
        answer = asked(
            Question(
                text=f"{warning}\n\nStart anyway and create these workspace copies?",
                options=_CONFIRMATION_OPTIONS,
            )
        )
        if not _confirmed(answer):
            print("Parallel Flame Chase startup cancelled; no new copies were created.")
            raise WorkspaceStartupCancelled("large workspace startup cancelled")

    def _open_run(self, objective: str, resume: bool) -> None:
        if resume:
            self._resume_run(objective)
        else:
            self._create_run(objective)
        self._initialize_mode_paths()
        self.bus = ReportBus(self.paths, self.lane_names)
        atomic_text(self.paths.root / "objective.md", objective + "\n")
        atomic_json(self.paths.workspace_map, self._workspace_map())
        self._validate_layout()

    def _planning_workspace(self, objective: str) -> Path:
        """Snapshot revised source so planning cannot mutate Lane 1."""
        workspace = (
            self.paths.shared / "planning-revisions" / task_fingerprint(objective)[:16]
        )
        if not workspace.exists():
            snapshot(self.source, workspace, self._workspace_statistics().total_bytes)
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
        for lane in self.lane_names:
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
        copies, description = self._workspace_copy_plan(resume=resume, revised=revised)
        self._confirm_workspace_copies(copies=copies, description=description)
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
