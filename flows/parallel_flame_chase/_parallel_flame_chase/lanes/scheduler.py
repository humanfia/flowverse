from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast

from hmz.flows import FlowParams

from ..core.models import LaneName, LaneReport
from ..core.utils import json_copy, now
from ..orchestration.state import RuntimeState
from ..persistence.checkpoints import checkpoint_report
from ..persistence.events import ReportBus
from ..persistence.leaderboard import with_submission
from ..persistence.workspace import checkpoint_state, validate_deliverable
from .prompts import lane_prompt
from .runtime import STOPPING, LaneRuntime, reason


class LaneScheduler(RuntimeState):
    session_protocol = (
        "Your partner alternates with you; leave durable work and evidence, "
        "not conversational memory."
    )

    def _actor_index(self, runtime: LaneRuntime, durable: dict[str, Any]) -> int:
        return int(durable.get("next_actor", runtime.actor_at)) % 2

    def _next_actor(self, runtime: LaneRuntime) -> int:
        return 1 - runtime.actor_at

    def _actor_role(self, lane: LaneName, actor_index: int) -> str:
        return f"{lane}-actor-{'a' if actor_index == 0 else 'b'}"

    def _initial_brief(self, lane: LaneName) -> dict[str, object]:
        plan = self._validate_plan(self.control["plan"])
        brief = next(item for item in plan.lanes if item.lane == lane)
        return brief.model_dump(mode="json")

    def _identity(self, lane: LaneName) -> dict[str, object]:
        turns = int(self.control["lanes"][lane].get("turns", 0))
        return {
            "version": 1,
            "run_id": self.control["run_id"],
            "lane": lane,
            "mission_id": None,
            "generation": turns,
        }

    def _lane_context(self, lane: LaneName) -> tuple[bool, dict[str, object] | None]:
        return True, None

    def _integration_item(self, lane: LaneName) -> dict[str, object] | None:
        return None

    def _previous_lane_report(self, lane: LaneName) -> dict[str, object] | None:
        return None

    def _unread_reports(
        self, lane: LaneName, cursors: dict[str, Any]
    ) -> tuple[list[dict[str, object]], dict[str, int]]:
        return self.bus.unread(lane, cursors)

    def _lane_instructions(self, lane: LaneName) -> str:
        return ""

    def _lane_ownership(self, lane: LaneName) -> str | None:
        return None

    def _after_lane_scheduled(self, runtime: LaneRuntime) -> None:
        pass

    def _observe_report(
        self,
        runtime: LaneRuntime,
        record: dict[str, object],
        report: LaneReport,
        *,
        candidate_became_best: bool,
    ) -> None:
        if report.status == "blocked":
            self.control["lanes"][runtime.lane]["blocked"] = True

    def _handle_pair_failure(self, runtime: LaneRuntime, error: str) -> None:
        self.control["lanes"][runtime.lane]["blocked"] = True

    def _stopped_turn_error(self, runtime: LaneRuntime) -> str | None:
        return None

    async def _take_turn(self, actor: Any, workspace: Any, prompt: str) -> LaneReport:
        return await self.lane_turn(
            prompt,
            agents={"actor": actor},
            envs={"place": workspace},
            params=FlowParams(),
        )

    async def _schedule_lane(self, runtime: LaneRuntime) -> None:
        lane = runtime.lane
        durable = cast("dict[str, Any]", self.control["lanes"][lane])
        if runtime.task is not None or durable.get("blocked"):
            return
        allowed, mission_document = self._lane_context(lane)
        if not allowed:
            return
        await self._validate_layout()
        paths = self.paths
        cursors = cast("dict[str, Any]", self.control["bus_cursors"])
        unread, acknowledgements = self._unread_reports(lane, cursors)
        identity = self._identity(lane)
        turn = int(durable.get("turns", 0)) + 1
        actor_index = self._actor_index(runtime, durable)
        runtime.actor_at = actor_index
        actor = runtime.actors[actor_index]
        prompt = lane_prompt(
            objective=cast("str", self.control["objective"]),
            lane=lane,
            actor_role=self._actor_role(lane, actor_index),
            turn=turn,
            workspace_map=self._workspace_map(),
            mission=mission_document,
            initial_brief=self._initial_brief(lane),
            unread_reports=unread,
            checkpoint_path=str(paths.checkpoint(lane)),
            artifact_root=str(paths.artifact_root(lane)),
            identity=identity,
            integration_item=self._integration_item(lane),
            candidate_board=json_copy(self.control["candidate_board"]),
            leaderboard_path=str(paths.leaderboard),
            runtime_status={
                "consecutive_failures": int(durable.get("consecutive_failures", 0)),
                "last_error": durable.get("last_error"),
            },
            skill=self.skill_name,
            previous_lane_report=self._previous_lane_report(lane),
            mode_instructions=self._lane_instructions(lane),
            ownership_instructions=self._lane_ownership(lane),
            session_protocol=self.session_protocol,
        )
        runtime.identity = identity
        runtime.pending_ack = acknowledgements
        runtime.checkpoint_before, _ = await checkpoint_state(
            self.store, paths.checkpoint(lane), text=False
        )
        self._after_lane_scheduled(runtime)
        runtime.task = asyncio.create_task(
            self._take_turn(actor, runtime.workspace, prompt),
            name=f"parallel-flame-{lane}",
        )

    def _report_header(
        self,
        runtime: LaneRuntime,
        *,
        recovered: bool,
    ) -> dict[str, object]:
        durable = self.control["lanes"][runtime.lane]
        return {
            "version": 1,
            "report_id": uuid.uuid4().hex,
            "at": now(),
            "run_id": self.control["run_id"],
            "lane": runtime.lane,
            "actor": "a" if runtime.actor_at == 0 else "b",
            "turn": int(durable.get("turns", 0)) + 1,
            "mission_id": runtime.identity.get("mission_id"),
            "generation": runtime.identity.get("generation"),
            "recovered_from_checkpoint": recovered,
        }

    async def _record_report(
        self,
        runtime: LaneRuntime,
        report: LaneReport,
        *,
        recovered: bool,
    ) -> None:
        lane = runtime.lane
        durable = cast("dict[str, Any]", self.control["lanes"][lane])
        artifacts = (
            await validate_deliverable(
                self.store, self.paths.artifact_root(lane), report.deliverable
            )
            if report.deliverable is not None
            else []
        )
        header = self._report_header(runtime, recovered=recovered)
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
                cast("dict[str, object]", self.control["candidate_board"]),
                record,
                cast("tuple[str, ...]", self.lane_names),
            )
        await self.bus.publish(lane, record)
        if updated_board is not None and candidate is not None:
            self.control["candidate_board"] = updated_board
            self._event(
                {
                    "at": header["at"],
                    "kind": (
                        "candidate_best_updated"
                        if became_best
                        else "candidate_submitted"
                    ),
                    "lane": lane,
                    "submission_id": candidate["submission_id"],
                    "metric": candidate["metric"],
                    "value": candidate["value"],
                    "direction": candidate["direction"],
                }
            )
        self.control["latest_reports"][lane] = json_copy(record)
        ReportBus.acknowledge(
            lane,
            cast("dict[str, Any]", self.control["bus_cursors"]),
            runtime.pending_ack,
        )
        durable["turns"] = int(durable.get("turns", 0)) + 1
        durable["next_actor"] = self._next_actor(runtime)
        durable["consecutive_failures"] = 0
        durable["last_error"] = None
        self._observe_report(
            runtime,
            record,
            report,
            candidate_became_best=became_best,
        )
        self.completed_turns += 1

    async def _record_failure(self, runtime: LaneRuntime, error: str) -> None:
        lane = runtime.lane
        durable = cast("dict[str, Any]", self.control["lanes"][lane])
        failure = {
            **self._report_header(runtime, recovered=False),
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
        await self.bus.publish(lane, failure)
        self.control["latest_reports"][lane] = json_copy(failure)
        durable["next_actor"] = self._next_actor(runtime)
        durable["consecutive_failures"] = (
            int(durable.get("consecutive_failures", 0)) + 1
        )
        durable["last_error"] = error[:2000]
        self._event(
            {
                "at": now(),
                "kind": "turn_failed",
                "lane": lane,
                "actor": failure["actor"],
                "mission_id": runtime.identity.get("mission_id"),
                "generation": runtime.identity.get("generation"),
                "error": error[:2000],
            }
        )
        if durable["consecutive_failures"] < 2:
            return
        self._handle_pair_failure(runtime, error)

    async def _collect_lane(self, runtime: LaneRuntime) -> None:
        task = runtime.task
        if task is None or not task.done():
            return
        runtime.task = None
        result: LaneReport | None = None
        recovered = False
        error: str | None = None
        try:
            result = task.result()
        except STOPPING:
            error = self._stopped_turn_error(runtime)
            if error is None:
                raise
        except Exception as why:  # noqa: BLE001
            error = reason(why)[:2000]
        if result is None:
            result = checkpoint_report(
                await checkpoint_state(self.store, self.paths.checkpoint(runtime.lane)),
                runtime.checkpoint_before,
                runtime.identity,
            )
            recovered = result is not None
        current_identity = self._identity(runtime.lane)
        stale = any(
            runtime.identity.get(key) != current_identity.get(key)
            for key in ("run_id", "lane", "mission_id", "generation")
        )
        if stale:
            self._event(
                {
                    "at": now(),
                    "kind": "stale_turn_discarded",
                    "lane": runtime.lane,
                    "identity": runtime.identity,
                }
            )
        elif result is not None and result.status != "turn_failed":
            try:
                await self._record_report(runtime, result, recovered=recovered)
            except (OSError, ValueError) as why:
                await self._record_failure(
                    runtime, f"invalid deliverable/report: {why}"
                )
        else:
            await self._record_failure(
                runtime,
                error
                or (result.summary if result is not None else None)
                or "actor returned no structured report",
            )
        runtime.pending_ack.clear()
        await self._persist()
