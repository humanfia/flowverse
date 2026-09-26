from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hmz.flows import (
    BudgetExceeded,
    EnvError,
    FlowCancelled,
    FlowParams,
    TempCloneBusy,
)

from .leaderboard import (
    empty_leaderboard,
    json_copy,
    validate_leaderboard,
    with_submission,
)
from .models import LANES, InitialPlan, LaneCheckpoint, LaneName, LaneReport
from .prompts import git_planning_prompt, lane_prompt, lane_protocol
from .repository import EXECUTABLES, REFUSED, TOOLS, GitRunPaths

if TYPE_CHECKING:
    from hmz.flows import Agent, Env, Flow, FlowContext

MODE = "git-pr"
SKILL = "parallel-flame-chase-git-pr"
STATE_VERSION = 2
PROTOCOL_VERSION = 1
GIT_SHA1_LENGTH = 40
IDENTITY_FIELDS = ("version", "run_id", "lane", "mission_id", "generation")
# The source is held through one temporary copy, under the ids the base Parallel Flame
# Chase uses too, so that the two never act on one source at once.
CLAIM_SCRATCH = "parallel_flame_chase-lock"
CLAIM = "owner"
# The run's state is kept one top-level key apiece, so that a save writes what changed.
CONTROL_KEYS = (
    "version",
    "protocol",
    "mode",
    "run_id",
    "run_root",
    "source",
    "objective",
    "task_fingerprint",
    "status",
    "created_at",
    "updated_at",
    "plan",
    "lanes",
    "bus_cursors",
    "latest_reports",
    "candidate_board",
    "events",
    "git_pr",
)
CONTINUATION_MARKERS = {
    "continue",
    "continue.",
    "resume",
    "resume.",
    "go on",
    "继续",
    "继续。",
}
_ACCEPTED_CONFIRMATIONS = frozenset(
    {"a", "1", "y", "yes", "是", "继续", "start anyway", "proceed"}
)
_TOOL_SOURCES = {
    name: Path(__file__).with_name(source).read_bytes()
    for name, source in TOOLS.items()
}


class WorkspaceStartupCancelled(Exception):
    pass


@dataclass(slots=True)
class Lane:
    name: LaneName
    actors: tuple[Agent, Agent]
    clone: Env
    actor_at: int = 0
    task: asyncio.Task[LaneReport] | None = None
    identity: dict[str, object] = field(default_factory=dict)
    pending_ack: dict[str, int] = field(default_factory=dict)
    checkpoint_before: object = None


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def task_fingerprint(task: str) -> str:
    normalized = task.strip().replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode()).hexdigest()


def _confirmed(answer: object) -> bool:
    if not isinstance(answer, str):
        return False
    normalized = answer.strip().casefold()
    return normalized in _ACCEPTED_CONFIRMATIONS or normalized.startswith(
        "a. start anyway"
    )


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class GitPRRuntime:
    """One run: three A/B lanes, a receipt fast path into a shadow main, and the source."""

    def __init__(
        self,
        task: str,
        *,
        agents: Any,
        envs: Any,
        params: Any,
        ctx: FlowContext,
        plan: Flow,
        lane_turn: Flow,
    ) -> None:
        if ctx.state is None:
            raise RuntimeError("the Git/PR runtime keeps resumable state")
        self.raw_task = task
        self.agents = agents
        self.workspace = envs["workspace"]
        self.params = params
        self.state = ctx.state
        self.plan = plan
        self.lane_turn = lane_turn
        self.source = str(self.workspace.workdir)
        self.control: dict[str, Any] = {}
        self.lanes: dict[LaneName, Lane] = {}
        self.allowed_paths: list[str] = []
        self.run_env: Any = None
        self.paths = GitRunPaths(Path())
        self._claims: Any = None
        self._saved: dict[str, str] = {}
        self._spent: BaseException | None = None

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        prepared = False
        try:
            await self._claim_source()
            await self.prepare()
            prepared = True
            print(
                f"parallel_flame_chase:{MODE} · run {self.control['run_id']} · "
                f"state {self.paths.root}"
            )
            while True:
                for lane in self.lanes.values():
                    await self._collect_lane(lane)
                await self._control_cycle()
                if self._spent is not None:
                    # A spent budget refuses every new turn; the turns under way finish
                    # first, as a graceful budget promises, and then the run stops.
                    if not any(lane.task for lane in self.lanes.values()):
                        raise self._spent
                else:
                    for lane in self.lanes.values():
                        await self._schedule_lane(lane)
                await asyncio.sleep(self.params.rest_seconds)
        except WorkspaceStartupCancelled:
            return
        except (asyncio.CancelledError, BudgetExceeded, FlowCancelled):
            if prepared:
                await self._record_exit("stopped")
            raise
        except BaseException:
            if prepared:
                await self._record_exit("failed")
            raise
        finally:
            running = [lane.task for lane in self.lanes.values() if lane.task]
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            if self._claims is not None:
                with contextlib.suppress(Exception):
                    await self._claims.destroy_temp_clone(CLAIM)

    async def _record_exit(self, status: str) -> None:
        # Turns that finished before the stop are still recorded.
        with contextlib.suppress(Exception):
            for lane in self.lanes.values():
                await self._collect_lane(lane)
        self.control["status"] = status
        try:
            await self._persist()
        except Exception:  # noqa: BLE001
            self._save_state()

    async def _claim_source(self) -> None:
        claims = await self.workspace.derive_scratch(CLAIM_SCRATCH)
        try:
            await claims.derive_temp_clone(CLAIM)
        except TempCloneBusy as why:
            raise RuntimeError(
                f"another parallel Flame Chase owns source workspace {self.source}"
            ) from why
        self._claims = claims

    async def prepare(self) -> None:
        objective, resume, revised = await self._resolve_objective()
        if resume:
            await self._resume_run(objective)
            if revised:
                await self._confirm_workspace_copies(
                    copies=1, description="1 revised-objective planning snapshot"
                )
            await self.run_env.write(
                str(self.paths.objective), (objective + "\n").encode()
            )
        else:
            await self._create_run(objective)
        await self._write_json(self.paths.workspace_map, self._workspace_map())
        await self._prepare_plan(objective, resume=resume, revised=revised)
        for number, lane in enumerate(LANES, start=1):
            self.lanes[lane] = Lane(
                lane,
                (
                    self.agents[f"lane_{number}_actor_a"],
                    self.agents[f"lane_{number}_actor_b"],
                ),
                await self.run_env.derive_subdir(subdir=f"private/{lane}"),
                actor_at=int(self.control["lanes"][lane].get("next_actor", 0)) % 2,
            )
        self.control["status"] = "running"
        await self._persist()

    def _previous(self) -> dict[str, Any]:
        return {key: self.state[key] for key in CONTROL_KEYS if key in self.state}

    def _save_state(self) -> None:
        for key in CONTROL_KEYS:
            text = json.dumps(
                self.control.get(key), ensure_ascii=False, sort_keys=True, default=str
            )
            if self._saved.get(key) != text:
                self.state[key] = json.loads(text)
                self._saved[key] = text

    async def _resolve_objective(self) -> tuple[str, bool, bool]:
        previous = self._previous()
        if (
            previous.get("version") == STATE_VERSION
            and previous.get("protocol") != PROTOCOL_VERSION
        ):
            raise ValueError("unsupported parallel Flame Chase state protocol")
        if previous.get("version") != STATE_VERSION:
            previous = {}
        marker = self.raw_task.strip().casefold() in CONTINUATION_MARKERS
        previous_source = previous.get("source") == self.source
        forced_fresh = self.params.resume_mode == "fresh"
        if marker:
            try:
                task_file: str | None = (await self.workspace.read("TASK.md")).decode()
            except EnvError:
                task_file = None
            if task_file is not None:
                objective = task_file.strip()
            elif previous_source and isinstance(previous.get("objective"), str):
                objective = previous["objective"].strip()
            else:
                raise ValueError("continue/resume requires TASK.md or resumable state")
            if not objective:
                raise ValueError("the resumed objective is empty")
            if forced_fresh or not previous_source or previous.get("mode") != MODE:
                return objective, False, False
            prior = previous.get("task_fingerprint")
            revised = isinstance(prior, str) and prior != task_fingerprint(objective)
            return objective, True, revised
        objective = self.raw_task.strip()
        if not objective:
            raise ValueError("task must not be empty")
        resume = (
            not forced_fresh
            and previous_source
            and previous.get("mode") == MODE
            and previous.get("task_fingerprint") == task_fingerprint(objective)
        )
        return objective, bool(resume), False

    async def _open_run_directory(self, run_id: str) -> None:
        self.run_env = await self.workspace.derive_scratch(
            f"parallel_flame_chase_git_pr-{run_id}"
        )
        self.paths = GitRunPaths(Path(str(self.run_env.workdir)))

    async def _install_tools(self, *, hook: bool) -> None:
        for name, data in _TOOL_SOURCES.items():
            await self.run_env.write(str(self.paths.bin / name), data)
        executables = [self.paths.bin / name for name in EXECUTABLES]
        if hook:
            installed = self.paths.central / "hooks" / "pre-receive"
            await self.run_env.write(str(installed), _TOOL_SOURCES["pfc-pre-receive"])
            executables.append(installed)
        await self._exec("chmod", "755", *(str(path) for path in executables))

    async def _create_run(self, objective: str) -> None:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{uuid.uuid4().hex[:10]}"
        await self._open_run_directory(run_id)
        self.control = {
            "version": STATE_VERSION,
            "protocol": PROTOCOL_VERSION,
            "mode": MODE,
            "run_id": run_id,
            "run_root": str(self.paths.root),
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
                for lane in LANES
            },
            "bus_cursors": {},
            "latest_reports": {},
            "candidate_board": empty_leaderboard(run_id),
            "events": [],
            "git_pr": {
                "observed_main_sha": None,
                "pending_comparison": None,
                "receipt_cursor": 0,
            },
        }
        await self._install_tools(hook=False)
        copies = len(LANES) + 2
        try:
            await self._confirm_workspace_copies(
                copies=copies,
                description=(
                    f"{copies} Git working trees (planning, each lane, and integration) "
                    "plus Git object storage"
                ),
            )
        except WorkspaceStartupCancelled:
            await self.workspace.destroy_scratch(
                f"parallel_flame_chase_git_pr-{run_id}"
            )
            raise
        await self.run_env.write(str(self.paths.objective), (objective + "\n").encode())
        created = await self._tool("init", self.source, "--run-id", run_id)
        self.control["git_pr"]["observed_main_sha"] = created["baseline"]
        self.allowed_paths = await self._store("meta", key="allowed_paths")

    async def _resume_run(self, objective: str) -> None:
        self.control = json_copy(self._previous())
        self._validate_resumable_control()
        self.control.update(
            objective=objective,
            task_fingerprint=task_fingerprint(objective),
            updated_at=now(),
        )
        await self._open_run_directory(self.control["run_id"])
        if str(self.paths.root) != self.control.get("run_root"):
            raise ValueError(
                "resumable run_root is outside this workspace's runtime home"
            )
        # The run's tools are this runtime's own: a resumed run runs the current ones.
        await self._install_tools(hook=False)
        try:
            await self._tool("check", "--run-id", self.control["run_id"])
        except RuntimeError as why:
            raise RuntimeError(
                f"resumable run is incomplete; refusing to recreate lost state: {why}"
            ) from why
        await self._install_tools(hook=True)
        self.allowed_paths = await self._store("meta", key="allowed_paths")

    def _validate_resumable_control(self) -> None:
        control = self.control
        if control.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("unsupported parallel Flame Chase state protocol")
        if control.get("mode") != MODE:
            raise ValueError("resumable state belongs to another flow mode")
        run_id = control.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("resumable state has no run_id")
        InitialPlan.model_validate(control.get("plan"))
        lanes = control.get("lanes")
        if not isinstance(lanes, dict) or set(lanes) != set(LANES):
            raise ValueError("resumable state has the wrong lane topology")
        for lane in LANES:
            held = lanes[lane]
            if not isinstance(held, dict):
                raise TypeError(f"{lane} resumable state is malformed")
            if held.get("next_actor") not in {0, 1} or isinstance(
                held.get("next_actor"), bool
            ):
                raise ValueError(f"{lane} next_actor must be 0 or 1")
            for name in ("turns", "consecutive_failures"):
                if not _count(held.get(name)):
                    raise ValueError(f"{lane} {name} must be a non-negative integer")
        for name in ("bus_cursors", "latest_reports"):
            if not isinstance(control.get(name), dict):
                raise TypeError(f"resumable state field {name!r} is malformed")
        if not isinstance(control.get("events"), list):
            raise TypeError("resumable state events are malformed")
        validate_leaderboard(control.get("candidate_board"), run_id)
        git_state = control.get("git_pr")
        if not isinstance(git_state, dict):
            raise TypeError("resumable Git control is malformed")
        observed = git_state.get("observed_main_sha")
        if not isinstance(observed, str) or len(observed) != GIT_SHA1_LENGTH:
            raise ValueError("resumable Git mode requires an observed main commit")
        if not _count(git_state.get("receipt_cursor")):
            raise ValueError("resumable receipt cursor must be non-negative")

    async def _confirm_workspace_copies(self, *, copies: int, description: str) -> None:
        stats = await self._tool("stats", self.source)
        files, size = int(stats["regular_files"]), int(stats["total_bytes"])
        file_threshold = self.params.workspace_file_warning_threshold
        byte_threshold = self.params.workspace_copy_warning_threshold_bytes
        estimated_bytes = size * copies
        if files <= file_threshold and estimated_bytes <= byte_threshold:
            return
        warning = (
            f"WARNING: source workspace {self.source} contains "
            f"{files:,} regular files and {size:,} apparent bytes.\n"
            f"Starting Parallel Flame Chase will create {description}; the rough "
            f"source-sized materialization estimate is {estimated_bytes:,} bytes before "
            "filesystem or Git optimizations.\n"
            f"Warning thresholds: {file_threshold:,} files or {byte_threshold:,} "
            "estimated bytes. No new workspace copy has been created yet."
        )
        print(warning)
        if not self.params.confirm_large_workspace_copies:
            print("Interactive confirmation is disabled; continuing startup.")
            return
        human = self.agents["human"]
        if human.away:
            print("No interactive confirmation is available; startup cancelled.")
            raise WorkspaceStartupCancelled(
                "large workspace startup requires confirmation"
            )
        session = await human.spawn(env=self.workspace)
        answer = await human.run(
            f"{warning}\n\nStart anyway and create these workspace copies?\n"
            "A. Start anyway\nB. Stop",
            session=session,
        )
        if not _confirmed(answer):
            print("Parallel Flame Chase startup cancelled; no new copies were created.")
            raise WorkspaceStartupCancelled("large workspace startup cancelled")

    async def _prepare_plan(
        self, objective: str, *, resume: bool, revised: bool
    ) -> None:
        replan = resume and revised
        if resume and self.control.get("plan") is not None and not replan:
            return
        snapshot = f"planning-{task_fingerprint(objective)[:16]}"
        workdir = (
            await self.workspace.derive_temp_clone(snapshot)
            if replan
            else await self.run_env.derive_subdir(subdir="shared/planning-workspace")
        )
        try:
            plan = await self._plan(objective, workdir)
        finally:
            if replan:
                await self.workspace.destroy_temp_clone(snapshot)
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
            self.control["lanes"][lane]["blocked"] = False
            self.control["lanes"][lane]["consecutive_failures"] = 0

    async def _plan(self, objective: str, workdir: Env) -> InitialPlan:
        prompt = git_planning_prompt(
            objective=objective, workspace_map=self._workspace_map(), skill=SKILL
        )
        failures: list[str] = []
        for attempt in range(1, 4):
            try:
                return await self.plan(
                    prompt,
                    agents={"orchestrator": self.agents["orchestrator"]},
                    envs={"workdir": workdir},
                    params=FlowParams(),
                )
            except (BudgetExceeded, FlowCancelled):
                raise
            except Exception as why:  # noqa: BLE001
                failures.append(
                    f"attempt {attempt}: {type(why).__name__}: {why}"[:1000]
                )
        raise RuntimeError(f"initial Git/PR plan failed: {failures}")

    # ---------------------------------------------------------------- run files

    async def _exec(self, *argv: str) -> str:
        code, out, err = await self.run_env.exec(list(argv))
        if code:
            raise RuntimeError(f"{argv[0]} exited {code}: {err.strip()}")
        return out

    async def _tool(self, command: str, *arguments: str) -> Any:
        """Runs one step of `pfc-runtime` in the run's directory.

        Raises:
          ValueError: If the step refused what it was given.
          RuntimeError: If it failed.
        """
        code, out, err = await self.run_env.exec(
            [
                str(self.paths.bin / "pfc-runtime"),
                "--root",
                str(self.paths.root),
                command,
                *arguments,
            ]
        )
        if code == REFUSED:
            raise ValueError(err.strip())
        if code:
            raise RuntimeError(
                f"pfc-runtime {command} exited {code}: {err.strip()[-4000:]}"
            )
        return json.loads(out)

    async def _store(self, method: str, **arguments: object) -> Any:
        return await self._tool(
            "store", method, json.dumps(arguments, ensure_ascii=False, default=str)
        )

    async def _write_json(self, path: Path, value: object) -> None:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
        await self.run_env.write(str(path), text.encode())

    def _factorial_cell(self) -> dict[str, object]:
        return {
            "git_pr_enabled": self.params.git_pr_enabled,
            "global_knowledge_enabled": self.params.global_knowledge_enabled,
            "experiment_memory_enabled": self.params.experiment_memory_enabled,
            "token_efficient_enabled": self.params.token_efficient_enabled,
            "main_update_monitor_enabled": self.params.main_update_monitor_enabled,
        }

    def _workspace_map(self) -> dict[str, object]:
        paths = self.paths
        return {
            "version": 1,
            "run_id": self.control["run_id"],
            "source": self.source,
            "shared": str(paths.shared),
            "lanes": {
                lane: {
                    "workspace": str(paths.lane(lane)),
                    "ownership": "isolated-writable-clone-and-equal-pr-author",
                }
                for lane in LANES
            },
            "artifact_roots": {lane: str(paths.artifact_root(lane)) for lane in LANES},
            "checkpoints": {lane: str(paths.checkpoint(lane)) for lane in LANES},
            "candidate_submissions": {
                "all_lanes_may_submit": True,
                "local_evaluator_only": True,
                "report_field": "submission",
                "requires_reconstructable_deliverable": True,
                "leaderboard": str(paths.leaderboard),
                "current": json_copy(self.control["candidate_board"]),
            },
            "remote_actions": "local-shadow-git-only",
            "integration": {
                "workspace": str(paths.integration),
                "ownership": "runtime-only-receipt-fast-path",
            },
            "factorial_cell": self._factorial_cell(),
            "git_pr": {
                "central": str(paths.central),
                "cli": str(paths.bin / "pfc"),
                "allowed_paths": self.allowed_paths,
                "ready_limit_per_lane": 1,
                "review_order": "lowest-receipted-cycles-first",
                "review_mode": "deterministic-receipt-fast-path",
            },
        }

    async def _persist(self) -> None:
        views = await self._tool("check", "--run-id", self.control["run_id"])
        self.control["updated_at"] = now()
        self._save_state()
        await self._write_json(self.paths.state_mirror, self.control)
        await self._write_json(
            self.paths.manifest,
            {
                "version": 1,
                "protocol": PROTOCOL_VERSION,
                "mode": MODE,
                "run_id": self.control.get("run_id"),
                "status": self.control.get("status"),
                "source": self.source,
                "objective_fingerprint": self.control.get("task_fingerprint"),
                "updated_at": now(),
                "lanes": json_copy(self.control.get("lanes", {})),
                "candidate_board": json_copy(self.control.get("candidate_board", {})),
                "remote_actions": "disabled",
                "factorial_cell": self._factorial_cell(),
                "git_pr": json_copy(self.control.get("git_pr")),
                "pull_requests": views["prs"],
                "official_ledger": str(self.paths.official_ledger),
            },
        )
        await self._write_json(self.paths.leaderboard, self.control["candidate_board"])
        await self._write_json(
            self.paths.official_ledger, {"version": 1, "entries": views["ledger"]}
        )

    async def _emit_system(
        self,
        *,
        targets: tuple[str, ...],
        kind: str,
        summary: str,
        payload: dict[str, object],
    ) -> None:
        record: dict[str, object] = {
            "version": 1,
            "report_id": uuid.uuid4().hex,
            "at": now(),
            "kind": kind,
            "summary": summary,
            "payload": payload,
            "audience": list(targets),
        }
        said = {"targets": list(targets), "record": record}
        await self._tool("system", json.dumps(said, ensure_ascii=False, default=str))

    # -------------------------------------------------------------------- lanes

    def _identity(self, lane: LaneName) -> dict[str, object]:
        return {
            "version": 1,
            "run_id": self.control["run_id"],
            "lane": lane,
            "mission_id": None,
            "generation": int(self.control["lanes"][lane].get("turns", 0)),
        }

    async def _schedule_lane(self, lane: Lane) -> None:
        durable = self.control["lanes"][lane.name]
        if lane.task is not None or durable.get("blocked"):
            return
        cursors = self.control["bus_cursors"].setdefault(lane.name, {})
        unread = await self._tool("unread", lane.name, json.dumps(cursors))
        identity = self._identity(lane.name)
        lane.actor_at = int(durable.get("next_actor", lane.actor_at)) % 2
        plan = InitialPlan.model_validate(self.control["plan"])
        brief = next(item for item in plan.lanes if item.lane == lane.name)
        previous = self.control["latest_reports"].get(lane.name)
        prompt = lane_prompt(
            objective=self.control["objective"],
            lane=lane.name,
            actor_role=f"{lane.name}-actor-{'a' if lane.actor_at == 0 else 'b'}",
            turn=int(durable.get("turns", 0)) + 1,
            workspace_map=self._workspace_map(),
            initial_brief=brief.model_dump(mode="json"),
            unread_reports=unread["reports"],
            checkpoint_path=str(self.paths.checkpoint(lane.name)),
            artifact_root=str(self.paths.artifact_root(lane.name)),
            identity=identity,
            runtime_status={
                "consecutive_failures": int(durable.get("consecutive_failures", 0)),
                "last_error": durable.get("last_error"),
            },
            candidate_board=json_copy(self.control["candidate_board"]),
            leaderboard_path=str(self.paths.leaderboard),
            skill=SKILL,
            previous_lane_report=(
                json_copy(previous) if isinstance(previous, dict) else None
            ),
            mode_instructions=lane_protocol(
                lane=lane.name,
                run_root=str(self.paths.root),
                cli=str(self.paths.bin / "pfc"),
                allowed_paths=self.allowed_paths,
            ),
        )
        lane.identity = identity
        lane.pending_ack = unread["acknowledgements"]
        lane.checkpoint_before = (await self._tool("checkpoint", lane.name))[
            "fingerprint"
        ]
        lane.task = asyncio.create_task(
            self._turn(lane.actors[lane.actor_at], lane.clone, prompt)
        )

    async def _turn(self, actor: Agent, clone: Env, prompt: str) -> LaneReport:
        return await self.lane_turn(
            prompt,
            agents={"actor": actor},
            envs={"workdir": clone},
            params=FlowParams(),
        )

    def _report_header(self, lane: Lane, *, recovered: bool) -> dict[str, object]:
        durable = self.control["lanes"][lane.name]
        return {
            "version": 1,
            "report_id": uuid.uuid4().hex,
            "at": now(),
            "run_id": self.control["run_id"],
            "lane": lane.name,
            "actor": "a" if lane.actor_at == 0 else "b",
            "turn": int(durable.get("turns", 0)) + 1,
            "mission_id": lane.identity.get("mission_id"),
            "generation": lane.identity.get("generation"),
            "recovered_from_checkpoint": recovered,
        }

    async def _collect_lane(self, lane: Lane) -> None:
        task = lane.task
        if task is None or not task.done():
            return
        lane.task = None
        if task.cancelled():
            return
        result: LaneReport | None = None
        error: str | None = None
        try:
            result = task.result()
        except (BudgetExceeded, FlowCancelled) as why:
            # The turn was refused or stopped for the run's budget: nothing to record.
            self._spent = self._spent or why
            return
        except Exception as why:  # noqa: BLE001
            error = f"{type(why).__name__}: {why}"[:2000]
        recovered = False
        if result is None:
            result = await self._checkpoint_report(lane)
            recovered = result is not None
        current = self._identity(lane.name)
        if any(
            lane.identity.get(key) != current.get(key)
            for key in ("run_id", "lane", "mission_id", "generation")
        ):
            self.control["events"].append(
                {
                    "at": now(),
                    "kind": "stale_turn_discarded",
                    "lane": lane.name,
                    "identity": lane.identity,
                }
            )
        elif result is not None and result.status != "turn_failed":
            try:
                await self._record_report(lane, result, recovered=recovered)
            except ValueError as why:
                await self._record_failure(lane, f"invalid deliverable/report: {why}")
        else:
            await self._record_failure(
                lane,
                error
                or (result.summary if result is not None else None)
                or "actor returned no structured report",
            )
        lane.pending_ack = {}
        await self._persist()

    async def _checkpoint_report(self, lane: Lane) -> LaneReport | None:
        held = await self._tool("checkpoint", lane.name)
        if held["text"] is None or held["fingerprint"] == lane.checkpoint_before:
            return None
        try:
            checkpoint = LaneCheckpoint.model_validate_json(held["text"])
        except ValueError:
            return None
        identity = checkpoint.identity.model_dump(mode="json")
        if any(identity.get(key) != lane.identity.get(key) for key in IDENTITY_FIELDS):
            return None
        return checkpoint.report

    async def _record_report(
        self, lane: Lane, report: LaneReport, *, recovered: bool
    ) -> None:
        durable = self.control["lanes"][lane.name]
        artifacts: list[dict[str, object]] = []
        if report.deliverable is not None:
            declared = [
                item.model_dump(mode="json") for item in report.deliverable.artifacts
            ]
            artifacts = await self._tool(
                "artifacts", lane.name, json.dumps(declared, ensure_ascii=False)
            )
        header = self._report_header(lane, recovered=recovered)
        record: dict[str, object] = {
            **header,
            **report.model_dump(mode="json"),
            "artifacts": artifacts,
        }
        updated_board: dict[str, object] | None = None
        candidate: dict[str, object] | None = None
        became_best = False
        if report.submission is not None:
            updated_board, candidate, became_best = with_submission(
                self.control["candidate_board"], record, LANES
            )
        said = {
            "record": record,
            "kind": "lane_report_recorded",
            "payload": {
                "report_id": record["report_id"],
                "status": report.status,
                "candidate_became_best": became_best,
            },
        }
        await self._tool(
            "report", lane.name, json.dumps(said, ensure_ascii=False, default=str)
        )
        if updated_board is not None and candidate is not None:
            self.control["candidate_board"] = updated_board
            self.control["events"].append(
                {
                    "at": header["at"],
                    "kind": (
                        "candidate_best_updated"
                        if became_best
                        else "candidate_submitted"
                    ),
                    "lane": lane.name,
                    "submission_id": candidate["submission_id"],
                    "metric": candidate["metric"],
                    "value": candidate["value"],
                    "direction": candidate["direction"],
                }
            )
        self.control["latest_reports"][lane.name] = json_copy(record)
        self.control["bus_cursors"].setdefault(lane.name, {}).update(lane.pending_ack)
        durable["turns"] = int(durable.get("turns", 0)) + 1
        durable["next_actor"] = 1 - lane.actor_at
        durable["consecutive_failures"] = 0
        durable["last_error"] = None
        if report.status == "blocked":
            durable["blocked"] = True

    async def _record_failure(self, lane: Lane, error: str) -> None:
        durable = self.control["lanes"][lane.name]
        failure = {
            **self._report_header(lane, recovered=False),
            "status": "turn_failed",
            "summary": error[:2000],
            "changes": [],
            "evidence": [],
            "tests": [],
            "risks": [],
            "next_step": "Retry with the alternating partner or report a concrete blocker.",
            "deliverable": None,
            "submission": None,
            "artifacts": [],
        }
        said = {
            "record": failure,
            "kind": "lane_turn_failed",
            "payload": {"error": error[:2000]},
        }
        await self._tool(
            "report", lane.name, json.dumps(said, ensure_ascii=False, default=str)
        )
        self.control["latest_reports"][lane.name] = json_copy(failure)
        durable["next_actor"] = 1 - lane.actor_at
        durable["consecutive_failures"] = (
            int(durable.get("consecutive_failures", 0)) + 1
        )
        durable["last_error"] = error[:2000]
        self.control["events"].append(
            {
                "at": now(),
                "kind": "turn_failed",
                "lane": lane.name,
                "actor": failure["actor"],
                "mission_id": lane.identity.get("mission_id"),
                "generation": lane.identity.get("generation"),
                "error": error[:2000],
            }
        )
        if durable["consecutive_failures"] >= 2:  # noqa: PLR2004 -- both partners
            durable["blocked"] = True

    # --------------------------------------------------------- the control cycle

    async def _control_cycle(self) -> None:
        git_state = self.control["git_pr"]
        polled = await self._tool(
            "poll", "--after", str(int(git_state.get("receipt_cursor", 0)))
        )
        changed = await self._scan_receipts(polled["receipts"], polled["end"])
        if polled["main"] != git_state["observed_main_sha"]:
            # Main moved without this runtime seeing it through: the PRs polled with it
            # are stale, so the fast path waits for the next cycle.
            changed = await self._observe_main() or changed
        elif (polled["active"] is not None or polled["ready"]) and (
            await self._process_fast_path(polled["active"], polled["ready"])
        ):
            changed = True
            await self._observe_main()
        if changed:
            await self._persist()

    async def _scan_receipts(self, receipts: list[dict[str, Any]], end: int) -> bool:
        git_state = self.control["git_pr"]
        cursor = int(git_state.get("receipt_cursor", 0))
        for receipt in receipts:
            if int(receipt["exit_code"]) == 0:
                continue
            target: str | None = None
            if receipt["lane"] in LANES:
                target = receipt["lane"]
            elif receipt.get("pr_id"):
                target = (await self._store("pr", pr_id=receipt["pr_id"]))["lane"]
            if target is not None:
                await self._emit_system(
                    targets=(target,),
                    kind="invalid_evaluation_receipt",
                    summary="An evaluation receipt failed and cannot qualify a PR.",
                    payload={
                        "receipt_id": receipt["id"],
                        "pr_id": receipt.get("pr_id"),
                        "commit_sha": receipt["commit_sha"],
                        "exit_code": receipt["exit_code"],
                    },
                )
        git_state["receipt_cursor"] = end
        return end != cursor

    async def _observe_main(self) -> bool:
        git_state = self.control["git_pr"]
        prior = git_state["observed_main_sha"]
        seen = await self._tool("observe", self.source, "--prior", prior)
        current = seen["main"]
        if current == prior:
            return False
        pr_id, changed = seen["pr_id"], seen["changed"]
        already_finalized = (await self._store("pr", pr_id=pr_id))["status"] == "merged"
        pending = git_state.get("pending_comparison")
        comparison = (
            pending
            if isinstance(pending, dict) and pending.get("pr_id") == pr_id
            else {
                "pr_id": pr_id,
                "review_mode": "receipt_fast_path",
                "recovered_after_restart": True,
            }
        )
        merged = await self._store(
            "finalize_merge",
            pr_id=pr_id,
            prior_main_sha=prior,
            merge_sha=current,
            comparison=comparison,
        )
        if not already_finalized:
            await self._emit_system(
                targets=LANES,
                kind="pr_merged",
                summary=f"{pr_id} was approved and published to the source workspace.",
                payload={
                    "pr_id": pr_id,
                    "author_lane": merged["lane"],
                    "merge_sha": current,
                    "changed_paths": changed,
                },
            )
            await self._emit_system(
                targets=(merged["lane"],),
                kind="receipt_fast_path_feedback",
                summary=comparison.get("summary", "PR merged"),
                payload={"pr_id": pr_id, "evidence": comparison.get("evidence", [])},
            )
        git_state["observed_main_sha"] = current
        git_state["pending_comparison"] = None
        await self._store(
            "record_telemetry",
            kind="main_published_to_source",
            payload={"pr_id": pr_id, "merge_sha": current, "changed_paths": changed},
        )
        return True

    async def _score(self, pr_id: str) -> tuple[int, str]:
        scored = await self._tool("score", pr_id)
        return int(scored["score"]), scored["receipt_id"]

    async def _reject_fast_path(self, pr: dict[str, Any], reason: str) -> None:
        rejected = await self._store("reject_pr", pr_id=pr["id"], reason=reason)
        await self._emit_system(
            targets=(rejected["lane"],),
            kind="pr_rejected",
            summary=reason,
            payload={"pr_id": rejected["id"], "fast_path": True},
        )

    async def _current_official_score(self) -> int | None:
        ledger = await self._store("ledger")
        if not ledger:
            return None
        score = ledger[-1]["comparison"].get("score")
        return score if _count(score) else None

    async def _process_fast_path(
        self, active: dict[str, Any] | None, ready: list[dict[str, Any]]
    ) -> bool:
        if active is None:
            ranked: list[tuple[int, str, str]] = []
            for candidate in ready:
                try:
                    score, _receipt = await self._score(candidate["id"])
                except ValueError as why:
                    activated = await self._store("activate_pr", pr_id=candidate["id"])
                    if activated is not None:
                        await self._reject_fast_path(
                            activated, f"Receipt fast-path rejected the PR: {why}"
                        )
                    return True
                ranked.append((score, candidate["ready_at"], candidate["id"]))
            if not ranked:
                return False
            active = await self._store("activate_pr", pr_id=min(ranked)[2])
            if active is None:
                return False
        try:
            selected_score, receipt_id = await self._score(active["id"])
        except ValueError as why:
            await self._reject_fast_path(
                active, f"Receipt fast-path rejected the PR: {why}"
            )
            return True
        current_score = await self._current_official_score()
        if current_score is not None and selected_score >= current_score:
            await self._reject_fast_path(
                active,
                f"Candidate cycles {selected_score} did not improve main {current_score}.",
            )
            return True
        git_state = self.control["git_pr"]
        prior = git_state["observed_main_sha"]
        # Kept before the push, so that a run stopped mid-merge still knows the score
        # of the main it finds when it picks up.
        git_state["pending_comparison"] = {
            "pr_id": active["id"],
            "review_mode": "receipt_fast_path",
            "score": selected_score,
            "prior_score": current_score,
            "receipt_id": receipt_id,
            "summary": (
                f"Auto-accepted exact evaluated tree at {selected_score} cycles"
                + (
                    f", improving {current_score}."
                    if current_score is not None
                    else "."
                )
            ),
            "evidence": [receipt_id],
        }
        self._save_state()
        try:
            merged = await self._tool("merge", active["id"], prior, active["head_sha"])
        except ValueError as why:
            git_state["pending_comparison"] = None
            await self._reject_fast_path(
                active, f"Deterministic integration failed: {str(why)[:1000]}"
            )
            return True
        await self._store(
            "record_telemetry",
            kind="pr_fast_path_published",
            payload={
                "pr_id": active["id"],
                "merge_sha": merged["merge_sha"],
                "score": selected_score,
                "receipt_id": receipt_id,
            },
            lane=active["lane"],
        )
        return True


__all__ = ["GitPRRuntime", "WorkspaceStartupCancelled"]
