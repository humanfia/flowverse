"""Lane dispatch, durable reports, failure isolation, and checkpoint recovery."""

from __future__ import annotations

import uuid
from typing import Any, cast

from hmz.flows import Stopped

from ..core.models import InitialPlan, LaneName, LaneReport
from ..core.utils import close_safely, json_copy, now
from ..orchestration.state import RuntimeState
from ..persistence.checkpoints import checkpoint_fingerprint, checkpoint_report
from ..persistence.events import ReportBus
from ..persistence.leaderboard import with_submission
from ..persistence.workspace import validate_deliverable
from .prompts import lane_prompt
from .runtime import LaneRuntime, run_lane_session


class LaneScheduler(RuntimeState):
    """Schedule alternating actors while the inherited state remains single-writer."""

    def _initial_brief(self, lane: LaneName) -> dict[str, object]:
        plan = InitialPlan.model_validate(self.control["plan"])
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
        """Return whether a lane may run and its optional specialized assignment."""
        return True, None

    def _integration_item(self, lane: LaneName) -> dict[str, object] | None:
        """Return a specialized Lane 1 handoff, if the mode defines one."""
        return None

    def _after_lane_scheduled(self, runtime: LaneRuntime) -> None:
        """Reset mode-specific ephemeral fields for a new turn."""

    def _observe_report(
        self,
        runtime: LaneRuntime,
        record: dict[str, object],
        report: LaneReport,
    ) -> None:
        """Apply mode-specific state transitions after a valid report."""
        if report.status == "blocked":
            self.control["lanes"][runtime.lane]["blocked"] = True

    def _handle_pair_failure(self, runtime: LaneRuntime, error: str) -> None:
        """Handle two consecutive actor failures for the active mode."""
        self.control["lanes"][runtime.lane]["blocked"] = True

    def _stopped_turn_error(self, runtime: LaneRuntime) -> str | None:
        """Translate a mode-owned session close into a reportable error."""
        return None

    def _schedule_lane(self, runtime: LaneRuntime) -> None:
        lane = runtime.lane
        durable = cast("dict[str, Any]", self.control["lanes"][lane])
        if runtime.future is not None or durable.get("blocked"):
            return
        allowed, mission_document = self._lane_context(lane)
        if not allowed:
            return
        self._validate_layout()
        cursors = cast("dict[str, Any]", self.control["bus_cursors"])
        unread, acknowledgements = self.bus.unread(lane, cursors)
        identity = self._identity(lane)
        turn = int(durable.get("turns", 0)) + 1
        actor_index = int(durable.get("next_actor", runtime.actor_at)) % 2
        runtime.actor_at = actor_index
        actor = runtime.actors[actor_index]
        prompt = lane_prompt(
            objective=cast("str", self.control["objective"]),
            lane=lane,
            actor_role=f"{lane}-actor-{'a' if actor_index == 0 else 'b'}",
            turn=turn,
            workspace_map=self._workspace_map(),
            mission=mission_document,
            initial_brief=self._initial_brief(lane),
            unread_reports=unread,
            checkpoint_path=str(self.paths.checkpoint(lane)),
            artifact_root=str(self.paths.artifact_root(lane)),
            identity=identity,
            integration_item=self._integration_item(lane),
            candidate_board=json_copy(self.control["candidate_board"]),
            leaderboard_path=str(self.paths.leaderboard),
            runtime_status={
                "consecutive_failures": int(durable.get("consecutive_failures", 0)),
                "last_error": durable.get("last_error"),
            },
            skill=self.skill_name,
        )
        runtime.identity = identity
        runtime.pending_ack = acknowledgements
        runtime.checkpoint_before = checkpoint_fingerprint(self.paths.checkpoint(lane))
        self._after_lane_scheduled(runtime)
        try:
            session = actor.new(cwd=runtime.workspace)
            runtime.session = session
            runtime.future = self.executor.submit(run_lane_session, session, prompt)
        except Stopped:
            raise
        except Exception as why:  # noqa: BLE001 - isolate actor startup failures
            close_safely(runtime.session)
            runtime.session = None
            self._record_failure(runtime, f"actor session could not start: {why}")
            self._persist()

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

    def _record_report(
        self,
        runtime: LaneRuntime,
        report: LaneReport,
        *,
        recovered: bool,
    ) -> None:
        lane = runtime.lane
        self._validate_layout()
        durable = cast("dict[str, Any]", self.control["lanes"][lane])
        artifacts = (
            validate_deliverable(self.paths.artifact_root(lane), report.deliverable)
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
                cast("dict[str, object]", self.control["candidate_board"]), record
            )
        self.bus.publish(lane, record)
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
        durable["next_actor"] = 1 - runtime.actor_at
        durable["consecutive_failures"] = 0
        durable["last_error"] = None
        self._observe_report(runtime, record, report)
        self.completed_turns += 1

    def _record_failure(self, runtime: LaneRuntime, error: str) -> None:
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
        self.bus.publish(lane, failure)
        self.control["latest_reports"][lane] = json_copy(failure)
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
                "actor": failure["actor"],
                "mission_id": runtime.identity.get("mission_id"),
                "generation": runtime.identity.get("generation"),
                "error": error[:2000],
            }
        )
        if durable["consecutive_failures"] < 2:
            return
        self._handle_pair_failure(runtime, error)

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
            error = self._stopped_turn_error(runtime)
            if error is None:
                raise
        except Exception as why:  # noqa: BLE001 - isolate actor backend failures
            error = f"{type(why).__name__}: {why}"[:2000]
        if result is None:
            result = checkpoint_report(
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
        if stale:
            self.control["events"].append(
                {
                    "at": now(),
                    "kind": "stale_turn_discarded",
                    "lane": runtime.lane,
                    "identity": runtime.identity,
                }
            )
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
        close_safely(runtime.session)
        runtime.session = None
        runtime.pending_ack.clear()
        self._persist()
