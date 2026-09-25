from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any, cast

from hmz.flows import EnvError, EnvPermissionDenied, FlowParams

from ..core.api import SKILL
from ..core.models import LANES, InitialPlan, LaneName
from ..core.utils import json_bytes, json_copy, now, task_fingerprint
from ..lanes.prompts import planning_prompt
from ..lanes.runtime import LaneRuntime
from ..persistence.events import ReportBus
from ..persistence.leaderboard import empty_leaderboard, validate_leaderboard
from ..persistence.workspace import (
    RunPaths,
    SourceLock,
    WorkspaceStats,
    commit_files,
    initialize_run,
    inspect_workspace,
    snapshot,
    validate_layout,
)

STATE_VERSION = 1
PROTOCOL_VERSION = 1
STATE_KEY = "control"
RUN_PREFIX = "parallel_flame_chase"
EVENT_LIMIT = 200
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


class WorkspaceStartupCancelled(Exception):
    pass


def _confirmed(answer: object) -> bool:
    if not isinstance(answer, str):
        return False
    normalized = answer.strip().casefold()
    if normalized in _ACCEPTED_CONFIRMATIONS:
        return True
    return normalized.startswith("a. start anyway")


class RuntimeState:
    mode_name = "base"
    skill_name = SKILL
    planning_cadence = (
        "This is the only coordinator turn; lanes will subsequently self-coordinate "
        "through durable reports."
    )
    orchestrator_role_name = "coordinator"
    replan_on_objective_revision = True
    lane_names: tuple[LaneName, ...] = LANES
    default_max_turns: int | None = None

    @staticmethod
    async def default_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    def __init__(
        self,
        agents: Any,
        envs: Any,
        task: str,
        params: Any,
        state: MutableMapping[str, Any] | None,
        *,
        planner: Any,
        lane_turn: Any,
        clock: Callable[[], dt.datetime] | None = None,
        sleeper: Callable[[float], Awaitable[object]] | None = None,
        max_turns: int | None = None,
    ) -> None:
        self.agents = agents
        self.workspace = envs["workspace"]
        self.raw_task = task
        self.params = params
        self.state: MutableMapping[str, Any] = state if state is not None else {}
        self.planner = planner
        self.lane_turn = lane_turn
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.sleeper = sleeper or self.default_sleep
        self.max_turns = self.default_max_turns if max_turns is None else max_turns
        self.source = str(self.workspace.workdir)
        self.previous: dict[str, Any] = {}
        self.control: dict[str, Any] = {}
        self.store: Any = None
        self.paths: RunPaths
        self.bus: ReportBus
        self.lock: SourceLock | None = None
        self.lanes: dict[LaneName, LaneRuntime] = {}
        self.lane_workspaces: dict[LaneName, Any] = {}
        self._workspace_stats: WorkspaceStats | None = None
        self.completed_turns = 0

    @property
    def _mode(self) -> str:
        return self.mode_name

    def _new_mode_control(self) -> dict[str, object]:
        return {}

    def _validate_mode_control(self) -> None:
        pass

    async def _prepare_mode(self, objective: str, *, revised: bool) -> None:
        pass

    def _before_persist(self) -> None:
        pass

    def _manifest_fields(self) -> dict[str, object]:
        return {}

    def _event(self, event: dict[str, object]) -> None:
        events = cast("list[dict[str, object]]", self.control["events"])
        events.append(event)
        del events[:-EVENT_LIMIT]

    async def _task_file(self) -> str | None:
        try:
            data = await self.workspace.read("TASK.md")
        except EnvPermissionDenied:
            raise
        except (FileNotFoundError, EnvError):
            return None
        return data.decode("utf-8")

    async def _resolve_objective(self) -> tuple[str, bool, bool]:
        stored = self.state[STATE_KEY] if STATE_KEY in self.state else {}
        if not isinstance(stored, dict):
            stored = {}
        stored = cast("dict[str, Any]", stored)
        if (
            stored.get("version") == STATE_VERSION
            and stored.get("protocol") != PROTOCOL_VERSION
        ):
            raise ValueError("unsupported parallel Flame Chase state protocol")
        marker = self.raw_task.strip().casefold() in CONTINUATION_MARKERS
        previous = stored if stored.get("version") == STATE_VERSION else {}
        self.previous = previous
        previous_source = previous.get("source") == self.source
        forced_fresh = self.params.resume_mode == "fresh"
        if marker:
            task_text = await self._task_file()
            if task_text is not None:
                objective = task_text.strip()
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
        control: dict[str, Any] = {
            "version": STATE_VERSION,
            "protocol": PROTOCOL_VERSION,
            "mode": self._mode,
            "run_id": run_id,
            "run_root": None,
            "source": self.source,
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

    def _resume_control(self, objective: str) -> None:
        self.control = json_copy(self.previous)
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

    def _workspace_map(self) -> dict[str, object]:
        paths = self.paths
        return {
            "version": 1,
            "run_id": self.control["run_id"],
            "source": self.source,
            "shared": str(paths.shared),
            "lanes": {
                lane: {
                    "workspace": (
                        self.source if lane == "lane-1" else str(paths.workspace(lane))
                    ),
                    "ownership": (
                        "original-source-and-integration"
                        if lane == "lane-1"
                        else "private-snapshot"
                    ),
                }
                for lane in self.lane_names
            },
            "artifact_roots": {
                lane: str(paths.artifact_root(lane)) for lane in self.lane_names
            },
            "checkpoints": {
                lane: str(paths.checkpoint(lane)) for lane in self.lane_names
            },
            "candidate_submissions": {
                "all_lanes_may_submit": True,
                "local_evaluator_only": True,
                "report_field": "submission",
                "requires_reconstructable_deliverable": True,
                "leaderboard": str(paths.leaderboard),
                "current": json_copy(self.control["candidate_board"]),
            },
            "remote_actions": "not-authorized-by-this-flow",
        }

    async def _plan(self, objective: str, place: Any) -> InitialPlan:
        prompt = planning_prompt(
            objective=objective,
            workspace_map=self._workspace_map(),
            skill=self.skill_name,
            role_name=self.orchestrator_role_name,
            cadence=self.planning_cadence,
        )
        return await self.planner(
            prompt,
            agents={"coordinator": self.agents["coordinator"]},
            envs={"place": place},
            params=FlowParams(),
        )

    def _validate_plan(self, value: object) -> Any:
        return InitialPlan.model_validate(value)

    def _workspace_copy_plan(self, *, resume: bool, revised: bool) -> tuple[int, str]:
        if not resume:
            private_lanes = tuple(lane for lane in self.lane_names if lane != "lane-1")
            copies = 1 + len(private_lanes)
            destinations = ", ".join(("planning", *private_lanes))
            return copies, f"{copies} workspace snapshots ({destinations})"
        if revised and self.replan_on_objective_revision:
            return 1, "1 revised-objective planning snapshot"
        return 0, ""

    async def _workspace_statistics(self) -> WorkspaceStats:
        if self._workspace_stats is None:
            self._workspace_stats = await inspect_workspace(self.workspace)
        return self._workspace_stats

    async def _confirm_workspace_copies(self, *, copies: int, description: str) -> None:
        if copies < 1:
            return
        stats = await self._workspace_statistics()
        file_threshold = self.params.workspace_file_warning_threshold
        byte_threshold = self.params.workspace_copy_warning_threshold_bytes
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
        if not self.params.confirm_large_workspace_copies:
            print("Interactive confirmation is disabled; continuing startup.")
            return
        human = self.agents.get("human")
        if human is None or human.away:
            print("No interactive confirmation is available; startup cancelled.")
            raise WorkspaceStartupCancelled(
                "large workspace startup requires confirmation"
            )
        session = await human.spawn(env=self.workspace)
        answer = await human.run(
            f"{warning}\n\nStart anyway and create these workspace copies?\n"
            f"Answer with one of: {', '.join(_CONFIRMATION_OPTIONS)}.",
            session=session,
        )
        if not _confirmed(answer):
            print("Parallel Flame Chase startup cancelled; no new copies were created.")
            raise WorkspaceStartupCancelled("large workspace startup cancelled")

    async def _validate_layout(self) -> None:
        await validate_layout(self.store, self.paths, self.lane_names)

    async def _commit(self, files: dict[Path, bytes]) -> None:
        await commit_files(self.store, self.paths, self.lane_names, files)

    async def _subdir(self, path: Path) -> Any:
        return await self.store.derive_subdir(
            subdir=path.relative_to(self.paths.root).as_posix()
        )

    async def _open_run(self, objective: str, resume: bool) -> None:
        self.store = await self.workspace.derive_scratch(
            f"{RUN_PREFIX}-{self.control['run_id']}"
        )
        self.paths = RunPaths(Path(str(self.store.workdir)))
        self.control["run_root"] = str(self.paths.root)
        stats = self._workspace_stats
        await initialize_run(
            self.workspace,
            self.paths,
            self.lane_names,
            fresh=not resume,
            size=None if stats is None else stats.total_bytes,
        )
        for lane in self.lane_names:
            self.lane_workspaces[lane] = (
                self.workspace
                if lane == "lane-1"
                else await self._subdir(self.paths.workspace(lane))
            )
        self.bus = ReportBus(self.store, self.lane_names)
        await self.bus.open()
        await self._commit(
            {
                self.paths.objective: (objective + "\n").encode(),
                self.paths.workspace_map: json_bytes(self._workspace_map()),
            }
        )

    async def _planning_workspace(self, objective: str) -> Any:
        revision = int(self.control.get("replans", 0)) + 1
        destination = (
            self.paths.planning_revisions
            / f"{task_fingerprint(objective)[:16]}-{revision}"
        )
        await snapshot(self.workspace, destination)
        return await self._subdir(destination)

    async def _prepare_plan(
        self, objective: str, *, resume: bool, revised: bool
    ) -> None:
        replan = resume and revised and self.replan_on_objective_revision
        needs_plan = not resume or self.control.get("plan") is None or replan
        if not needs_plan:
            return
        place = (
            await self._planning_workspace(objective)
            if resume and revised
            else await self._subdir(self.paths.planning)
        )
        plan = await self._plan(objective, place)
        self.control["plan"] = plan.model_dump(mode="json")
        if not replan:
            return
        self.control["replans"] = int(self.control.get("replans", 0)) + 1
        self._event(
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
        for lane in self.lane_names:
            lane_state = cast("dict[str, Any]", self.control["lanes"][lane])
            role = lane.replace("-", "_")
            self.lanes[lane] = self._make_lane_runtime(
                lane=lane,
                actors=(self.agents[f"{role}_actor_a"], self.agents[f"{role}_actor_b"]),
                workspace=self.lane_workspaces[lane],
                actor_at=int(lane_state.get("next_actor", 0)) % 2,
            )

    def _make_lane_runtime(self, **fields: Any) -> LaneRuntime:
        return LaneRuntime(**fields)

    async def prepare(self) -> None:
        objective, resume, revised = await self._resolve_objective()
        copies, description = self._workspace_copy_plan(resume=resume, revised=revised)
        await self._confirm_workspace_copies(copies=copies, description=description)
        if resume:
            self._resume_control(objective)
        else:
            self.control = self._new_control(objective)
        self.lock = SourceLock(self.workspace, cast("str", self.control["run_id"]))
        await self.lock.acquire()
        await self._open_run(objective, resume)
        await self._prepare_plan(objective, resume=resume, revised=revised)
        await self._prepare_mode(objective, revised=revised)
        self._prepare_lanes()
        self.control["status"] = "running"
        await self._persist()

    async def release(self) -> None:
        lock, self.lock = self.lock, None
        if lock is not None:
            await lock.release()

    def _manifest(self) -> dict[str, object]:
        manifest: dict[str, object] = {
            "version": 1,
            "protocol": PROTOCOL_VERSION,
            "mode": self._mode,
            "run_id": self.control.get("run_id"),
            "status": self.control.get("status"),
            "source": self.source,
            "objective_fingerprint": self.control.get("task_fingerprint"),
            "updated_at": now(),
            "lanes": json_copy(self.control.get("lanes", {})),
            "candidate_board": json_copy(self.control.get("candidate_board", {})),
            "remote_actions": "disabled",
        }
        manifest.update(self._manifest_fields())
        return manifest

    async def _persist(self) -> None:
        self._before_persist()
        self.control["updated_at"] = now()
        self.state[STATE_KEY] = self.control
        await self._commit(
            {
                self.paths.state_mirror: json_bytes(self.control),
                self.paths.manifest: json_bytes(self._manifest()),
                self.paths.leaderboard: json_bytes(self.control["candidate_board"]),
            }
        )
