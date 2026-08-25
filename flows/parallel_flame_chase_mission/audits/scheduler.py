"""Mission audit quiescing, evidence projection, decisions, and external ingress."""

from __future__ import annotations

import stat
import time
from pathlib import Path
from typing import Any, cast

from _parallel_flame_chase.core.utils import atomic_json, close_safely, json_copy, now
from _parallel_flame_chase.persistence.checkpoints import read_checkpoint
from _parallel_flame_chase.persistence.workspace import (
    artifacts_still_match,
    inspect_workspace,
    snapshot,
)
from hmz.flows import Session, Stopped

from ..coordination.controller import MissionController
from ..coordination.events import ExternalEventReader
from ..coordination.models import AuditDecision
from ..runtime.scheduler import MissionScheduler
from .prompts import AUDIT_PROMPT_MAX_CHARS, compact_audit_packet
from .runtime import CoordinatorRuntime, run_audit_session


class AuditScheduler(MissionScheduler):
    """Pause only audit targets and apply one revision-bound coordinator decision."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.coordinator = CoordinatorRuntime()

    @property
    def _audit_root(self) -> Path:
        return self.paths.shared / "audits"

    def _initialize_mode_paths(self) -> None:
        self._audit_root.mkdir(parents=True, exist_ok=True)

    def _validate_mode_layout(self) -> None:
        try:
            info = self._audit_root.lstat()
        except OSError as why:
            raise RuntimeError(
                f"runtime directory is missing: {self._audit_root}"
            ) from why
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(
                f"runtime directory was replaced or linked: {self._audit_root}"
            )

    def _close_sessions(self) -> None:
        super()._close_sessions()
        close_safely(self.coordinator.session)

    def _quiesce_targets(self) -> None:
        controller = self.controller
        if controller is None:
            return
        audit = controller.active_audit()
        if audit is None:
            return
        revision = int(audit["revision"])
        grace = float(getattr(self.config, "interrupt_grace_seconds", 60.0))
        for lane in controller.targets():
            runtime = self._mission_lane(self.lanes[lane])
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
                    except Exception as why:  # noqa: BLE001 - best-effort interjection
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
                    close_safely(runtime.session)
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
            checkpoint = read_checkpoint(
                self.paths.checkpoint(lane),
                self._identity(lane),
            )
            evidence = {
                "latest_report": json_copy(self.control["latest_reports"].get(lane)),
                "checkpoint": (
                    checkpoint.model_dump(mode="json")
                    if checkpoint is not None
                    else None
                ),
                "runtime_identity": json_copy(runtime.identity),
                "interruption": interruption,
            }
            controller.mark_quiesced(lane, interruption, evidence)
            runtime.quiesced_revision = revision
            self._persist()

    def _audit_cwd_and_packet(self) -> tuple[Path, dict[str, object]]:
        self._validate_layout()
        controller = self._controller()
        audit = cast("dict[str, Any]", controller.active_audit())
        directory = self._audit_root / cast("str", audit["id"])
        directory.mkdir(parents=True, exist_ok=True)
        packet = controller.decision_packet(
            cast("dict[str, object]", self.control["latest_reports"]),
            self._manifest(),
        )
        revision = int(audit["revision"])
        packet_path = directory / f"packet-r{revision}.json"
        atomic_json(packet_path, packet)
        coordinator_packet = compact_audit_packet(
            packet,
            max_prompt_chars=AUDIT_PROMPT_MAX_CHARS,
        )
        projection = coordinator_packet.get("_prompt_projection")
        if isinstance(projection, dict):
            projection["full_packet_file"] = str(packet_path)
        coordinator_packet = compact_audit_packet(
            coordinator_packet,
            max_prompt_chars=AUDIT_PROMPT_MAX_CHARS,
        )
        atomic_json(
            directory / f"coordinator-packet-r{revision}.json",
            coordinator_packet,
        )
        if audit["scope"] != "global":
            return directory, coordinator_packet
        source_snapshot = directory / f"source-r{revision}"
        if not source_snapshot.exists():
            snapshot(self.source, source_snapshot, inspect_workspace(self.source))
        return source_snapshot, coordinator_packet

    def _start_audit_decision(self) -> None:
        controller = self.controller
        if controller is None or self.coordinator.future is not None:
            return
        if (
            not controller.ready_for_decision()
            or time.monotonic() < self.coordinator.retry_after
        ):
            return
        deciding = controller.deciding()
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
                run_audit_session,
                session,
                packet,
            )
        except Stopped:
            raise
        except Exception as why:  # noqa: BLE001 - retry coordinator startup failures
            close_safely(self.coordinator.session)
            self.coordinator.session = None
            controller.record_attempt(
                {
                    "at": now(),
                    "audit_id": audit_id,
                    "revision": revision,
                    "error": f"coordinator session could not start: {why}"[:2000],
                }
            )
            self.coordinator.retry_later()
        self._persist()

    def _accepted_artifacts_error(self, decision: AuditDecision) -> str | None:
        controller = self.controller
        if controller is None:
            return None
        for item in decision.lanes:
            if item.verdict != "accept":
                continue
            mission = controller.current_mission(item.lane)
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
        session, self.coordinator.session = self.coordinator.session, None
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
        except Exception as why:  # noqa: BLE001 - retry coordinator backend failures
            decision = None
            attempts: list[dict[str, object]] = [
                {"at": now(), "error": f"{type(why).__name__}: {why}"[:2000]}
            ]
        close_safely(session)
        controller = self._controller()
        for attempt in attempts:
            controller.record_attempt(attempt)
        active = controller.active_audit()
        stale = active is None or (
            self.coordinator.audit_id != active["id"]
            or self.coordinator.revision != active["revision"]
        )
        if stale:
            controller.record_attempt(
                {
                    "at": now(),
                    "valid": False,
                    "audit_id": self.coordinator.audit_id,
                    "revision": self.coordinator.revision,
                    "error": "decision discarded because a newer audit revision is active",
                }
            )
            self.coordinator.reset_retry()
            self._persist()
            return
        error = "coordinator exhausted proposal and repair attempts"
        if decision is not None:
            error = controller.validate_decision(
                decision
            ) or self._accepted_artifacts_error(decision)
            if error is None:
                self._apply_audit_decision(controller, decision)
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
        self.coordinator.retry_later()
        self._persist()

    def _apply_audit_decision(
        self,
        controller: MissionController,
        decision: AuditDecision,
    ) -> None:
        for lane in controller.apply(decision):
            runtime = self._mission_lane(self.lanes[lane])
            runtime.quiesced_revision = None
            if runtime.future is None:
                runtime.interjected_at = None
                runtime.closed_for_audit = False
            durable = self.control["lanes"][lane]
            durable["consecutive_failures"] = 0
            durable["blocked"] = False
        self.coordinator.reset_retry()
        self._persist()

    def _supersede_stale_coordinator(self) -> None:
        controller = self.controller
        if controller is None or self.coordinator.future is None:
            return
        audit = controller.active_audit()
        if audit is None or (
            self.coordinator.audit_id == audit["id"]
            and self.coordinator.revision == audit["revision"]
        ):
            return
        if not self.coordinator.closed_for_stale_revision:
            close_safely(self.coordinator.session)
            self.coordinator.closed_for_stale_revision = True

    def _read_external_events(self) -> None:
        controller = self.controller
        if controller is None:
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
            controller.observe_external(event)
        if events or errors:
            self._persist()

    def _control_cycle(self) -> None:
        controller = self._controller()
        self._read_external_events()
        controller.tick()
        self._supersede_stale_coordinator()
        self._quiesce_targets()
        self._collect_audit_decision()
        self._start_audit_decision()
