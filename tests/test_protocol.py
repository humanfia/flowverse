from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from _parallel_flame_chase.missions import MissionController
from _parallel_flame_chase.models import (
    AcceptDecision,
    ArtifactRef,
    AuditDecision,
    CheckpointIdentity,
    Deliverable,
    InitialPlan,
    IntegrationDirective,
    LaneBrief,
    LaneCheckpoint,
    LaneReport,
    MissionSpec,
)
from _parallel_flame_chase.prompts import (
    AUDIT_PROMPT_MAX_CHARS,
    AUDIT_PROMPT_RETRY_MAX_CHARS,
    audit_prompt,
    compact_audit_packet,
)
from _parallel_flame_chase.runtime import (
    _checkpoint_report,
    _run_audit_session,
    _run_lane_session,
)
from _parallel_flame_chase.storage import (
    EXTERNAL_LINE_LIMIT,
    ExternalEventReader,
    ReportBus,
    RunPaths,
    SourceLock,
    artifacts_still_match,
    initialize_paths,
    validate_deliverable,
    validate_runtime_layout,
)


def mission(title: str, approach: str, *, kind: str = "research") -> MissionSpec:
    return MissionSpec(
        title=title,
        objective=f"Test {title}",
        success_criteria=[f"Produce evidence for {title}"],
        kind=kind,
        approach_class=approach,
        change_scale="integration" if kind == "integration" else "component",
        information_question=f"Does {title} work?",
    )


def plan() -> InitialPlan:
    return InitialPlan(
        lanes=[
            LaneBrief(
                lane="lane-1", mission=mission("integrated baseline", "baseline")
            ),
            LaneBrief(lane="lane-2", mission=mission("algorithm probe", "algorithm")),
            LaneBrief(lane="lane-3", mission=mission("validation probe", "validation")),
        ]
    )


def test_agent_output_schemas_require_every_object_property() -> None:
    """Codex strict structured output rejects optional object properties."""

    def inspect(value: object, path: str) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                assert set(value.get("required", [])) == set(properties), path
            for key, child in value.items():
                inspect(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{path}/{index}")

    for output in (InitialPlan, LaneReport, AuditDecision):
        inspect(output.model_json_schema(), output.__name__)

    audit_schema = json.dumps(AuditDecision.model_json_schema())
    assert '"oneOf"' not in audit_schema
    assert '"anyOf"' in audit_schema


def controller(clock: list[dt.datetime] | None = None) -> MissionController:
    held = clock or [dt.datetime(2026, 8, 23, tzinfo=dt.UTC)]
    instance = MissionController(
        None,
        run_id="run-1",
        objective="Improve the repository",
        global_audit_hours=6.0,
        default_deadline_hours=6.0,
        default_max_turns=6,
        clock=lambda: held[0],
    )
    instance.bootstrap(plan())
    return instance


def ready_record(instance: MissionController, lane: str) -> dict[str, object]:
    identity = instance.identity(lane)  # type: ignore[arg-type]
    report = LaneReport(
        status="deliverable_ready",
        summary="A reconstructable candidate passed its local checks.",
        evidence=["test suite passed"],
        deliverable=Deliverable(
            title="candidate",
            approach_class="algorithm",
            artifacts=[
                ArtifactRef(path="candidate.patch", description="portable patch")
            ],
            integration_notes="Apply the patch and rerun the test suite.",
        ),
    )
    return {
        **identity,
        **report.model_dump(mode="json"),
        "artifacts": [
            {
                "path": "candidate.patch",
                "size": 5,
                "sha256": "a" * 64,
                "description": "portable patch",
            }
        ],
    }


def quiesce_all(instance: MissionController) -> None:
    for lane in instance.targets():
        instance.mark_quiesced(
            lane,
            {"method": "natural-boundary"},
            {"latest_report": None},
        )


def test_targeted_accept_queues_and_lane_one_resumes_after_integration() -> None:
    held = controller()
    lane_one_research = held.current_mission("lane-1")["id"]
    held.observe("lane-2", ready_record(held, "lane-2"))
    audit = held.active_audit()
    assert audit is not None
    assert audit["scope"] == "targeted"
    assert audit["targets"] == ["lane-2"]
    quiesce_all(held)
    decision = AuditDecision(
        audit_id=audit["id"],
        revision=audit["revision"],
        lanes=[
            AcceptDecision(
                lane="lane-2",
                verdict="accept",
                reason="The package is explicit and validated.",
                integration=IntegrationDirective(
                    priority=10,
                    objective="Integrate and regression-test the candidate.",
                    success_criteria=[
                        "Candidate is integrated",
                        "Regressions are absent",
                    ],
                ),
                next_mission=mission("orthogonal follow-up", "data"),
            )
        ],
    )
    assert held.validate_decision(decision) is None
    assert held.apply(decision) == ("lane-2",)
    queued = held.queued_integration()
    assert queued is not None
    held.activate_integration(queued["id"])
    integration = held.current_mission("lane-1")
    assert integration["spec"]["kind"] == "integration"
    assert integration["resume_mission_id"] == lane_one_research
    held.observe("lane-1", ready_record(held, "lane-1"))
    global_audit = held.active_audit()
    assert global_audit is not None
    assert global_audit["scope"] == "global"
    quiesce_all(held)
    finish = AuditDecision(
        audit_id=global_audit["id"],
        revision=global_audit["revision"],
        lanes=[
            AcceptDecision(
                lane="lane-1",
                verdict="accept",
                reason="Integrated result passed regression checks.",
            ),
            {
                "lane": "lane-2",
                "verdict": "continue",
                "reason": "Its new orthogonal mission remains useful.",
            },
            {
                "lane": "lane-3",
                "verdict": "continue",
                "reason": "Validation work remains useful.",
            },
        ],
    )
    assert held.validate_decision(finish) is None
    held.apply(finish)
    assert held.current_mission("lane-1")["id"] == lane_one_research
    queue_item = held.data["integration_queue"][0]
    assert queue_item["status"] == "accepted"


def test_audit_coalesces_targets_and_rejects_stale_revision() -> None:
    held = controller()
    identity = held.identity("lane-2")
    held.observe(
        "lane-2",
        {
            **identity,
            "status": "no_result",
            "summary": "The probe falsified its hypothesis.",
            "evidence": ["controlled comparison was negative"],
        },
    )
    first = held.active_audit()
    assert first is not None
    stale = AuditDecision(
        audit_id=first["id"],
        revision=first["revision"],
        lanes=[
            {
                "lane": "lane-2",
                "verdict": "redirect",
                "reason": "The tested direction is exhausted.",
                "replacement": mission("replacement", "architecture"),
            }
        ],
    )
    held.trigger(
        "manual-review",
        {"event_id": "event-2", "at": "2026-08-23T00:00:00Z"},
        scope="targeted",
        targets=["lane-3"],
    )
    merged = held.active_audit()
    assert merged is not None
    assert merged["targets"] == ["lane-2", "lane-3"]
    assert merged["revision"] == first["revision"]
    assert "revision must be" in (held.validate_decision(stale) or "")


def test_failed_integration_can_be_abandoned_for_new_lane_one_research() -> None:
    held = controller()
    held.observe("lane-2", ready_record(held, "lane-2"))
    audit = held.active_audit()
    assert audit is not None
    quiesce_all(held)
    held.apply(
        AuditDecision(
            audit_id=audit["id"],
            revision=audit["revision"],
            lanes=[
                AcceptDecision(
                    lane="lane-2",
                    verdict="accept",
                    reason="The package is reproducible.",
                    integration=IntegrationDirective(
                        objective="Try the candidate in the source.",
                        success_criteria=["Run its regression checks"],
                    ),
                    next_mission=mission("lane-2 successor", "data"),
                )
            ],
        )
    )
    queued = held.queued_integration()
    assert queued is not None
    held.activate_integration(queued["id"])
    integration = held.current_mission("lane-1")
    held.observe(
        "lane-1",
        {
            **held.identity("lane-1"),
            "status": "no_result",
            "summary": "Integration regressed the source and cannot be retained.",
            "evidence": ["regression check failed"],
        },
    )
    failed_audit = held.active_audit()
    assert failed_audit is not None
    quiesce_all(held)
    redirect = AuditDecision(
        audit_id=failed_audit["id"],
        revision=failed_audit["revision"],
        lanes=[
            {
                "lane": "lane-1",
                "verdict": "redirect",
                "reason": "The accepted package is incompatible with the source.",
                "replacement": mission("new integrated research", "architecture"),
            }
        ],
    )
    assert held.validate_decision(redirect) is None
    held.apply(redirect)
    assert held.current_mission("lane-1")["spec"]["kind"] == "research"
    assert held.data["integration_queue"][0]["status"] == "rejected"
    paused = next(
        item
        for item in held.data["missions"]
        if item["id"] == integration["resume_mission_id"]
    )
    assert paused["status"] == "rejected"


def test_periodic_global_audit_does_not_wait_for_lane_deadlines() -> None:
    clock = [dt.datetime(2026, 8, 23, tzinfo=dt.UTC)]
    held = controller(clock)
    clock[0] += dt.timedelta(hours=6, seconds=1)
    held.tick()
    audit = held.active_audit()
    assert audit is not None
    assert audit["scope"] == "global"
    assert audit["targets"] == ["lane-1", "lane-2", "lane-3"]


def test_report_bus_redelivers_until_acknowledged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    paths = RunPaths(tmp_path / "run", source)
    initialize_paths(paths, make_snapshots=False)
    bus = ReportBus(paths)
    bus.publish("lane-2", {"status": "progress", "summary": "useful evidence"})
    cursors: dict[str, object] = {}
    first, acknowledgements = bus.unread("lane-1", cursors)
    second, _ = bus.unread("lane-1", cursors)
    assert first == second
    assert first[0]["source_lane"] == "lane-2"
    bus.acknowledge("lane-1", cursors, acknowledgements)
    assert bus.unread("lane-1", cursors)[0] == []


def test_artifact_package_is_explicit_hashed_and_immutable(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    candidate = root / "candidate.patch"
    candidate.write_text("patch", encoding="utf-8")
    deliverable = Deliverable(
        title="candidate",
        approach_class="algorithm",
        artifacts=[ArtifactRef(path="candidate.patch", description="portable patch")],
        integration_notes="Apply and test.",
    )
    recorded = validate_deliverable(root, deliverable)
    assert artifacts_still_match(root, recorded)
    candidate.write_text("changed", encoding="utf-8")
    assert not artifacts_still_match(root, recorded)
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    escaping = deliverable.model_copy(
        update={"artifacts": [ArtifactRef(path="escape", description="bad link")]}
    )
    with pytest.raises(ValueError, match="escapes|regular file"):
        validate_deliverable(root, escaping)


def test_external_ingress_is_versioned_deduplicated_and_run_bound(
    tmp_path: Path,
) -> None:
    stream = tmp_path / "events.jsonl"
    valid = {
        "version": 1,
        "event_id": "event-1",
        "run_id": "run-1",
        "at": "2026-08-23T00:00:00Z",
        "kind": "review_requested",
        "scope": "targeted",
        "targets": ["lane-2"],
        "summary": "Adapter observed a useful boundary.",
        "evidence": ["external metric changed"],
    }
    wrong_run = {**valid, "event_id": "event-2", "run_id": "another-run"}
    stream.write_text(
        "\n".join(
            [json.dumps(valid), json.dumps(valid), json.dumps(wrong_run), "not-json"]
        )
        + "\n",
        encoding="utf-8",
    )
    events, errors, cursor, seen = ExternalEventReader(stream).read("run-1", None, [])
    assert [event.event_id for event in events] == ["event-1"]
    assert errors
    assert cursor["offset"] == stream.stat().st_size
    assert seen == ["event-1"]


def test_external_ingress_skips_oversized_lines_and_waits_for_truncated_lines(
    tmp_path: Path,
) -> None:
    stream = tmp_path / "events.jsonl"
    valid = {
        "version": 1,
        "event_id": "event-after-oversized",
        "run_id": "run-1",
        "at": "2026-08-23T00:00:00Z",
        "kind": "progress",
        "summary": "A complete event after an oversized record.",
    }
    stream.write_bytes(
        b"x" * (EXTERNAL_LINE_LIMIT + 10) + b"\n" + json.dumps(valid).encode()
    )
    events, errors, cursor, seen = ExternalEventReader(stream).read("run-1", None, [])
    assert events == []
    assert any(error["error"] == "event line is oversized" for error in errors)
    assert any(
        error["error"] == "event line is currently truncated" for error in errors
    )
    assert cursor["offset"] == EXTERNAL_LINE_LIMIT + 11
    with stream.open("ab") as handle:
        handle.write(b"\n")
    events, errors, cursor, seen = ExternalEventReader(stream).read(
        "run-1", cursor, seen
    )
    assert [event.event_id for event in events] == ["event-after-oversized"]
    assert errors == []


class AuditSequence:
    def __init__(self, decisions: list[AuditDecision]) -> None:
        self.decisions = decisions
        self.prompts: list[str] = []

    def __call__(
        self, prompt: str, *, suppress: bool, schema: type[Any]
    ) -> AuditDecision:
        assert suppress is False
        assert schema is AuditDecision
        self.prompts.append(prompt)
        return self.decisions.pop(0)


class LaneRepairSequence:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *, suppress: bool, schema: type[Any]) -> LaneReport:
        assert suppress is False
        assert schema is LaneReport
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            raise ValueError(
                "the turn did not answer as a LaneReport: progress carried a deliverable"
            )
        return LaneReport(
            status="deliverable_ready",
            summary="The same session corrected its report.",
            deliverable=Deliverable(
                title="candidate",
                approach_class="algorithm",
                artifacts=[
                    ArtifactRef(path="candidate.patch", description="portable patch")
                ],
                integration_notes="Apply and validate the candidate.",
            ),
        )


def test_lane_repairs_invalid_report_in_the_same_session() -> None:
    session = LaneRepairSequence()
    report = _run_lane_session(session, "Do the assigned work.")
    assert report is not None
    assert report.status == "deliverable_ready"
    assert len(session.prompts) == 2
    assert "rejected by the LaneReport protocol" in session.prompts[1]


def test_audit_repairs_semantic_error_in_same_session_without_fallback() -> None:
    held = controller()
    identity = held.identity("lane-2")
    held.observe(
        "lane-2",
        {
            **identity,
            "status": "no_result",
            "summary": "The hypothesis was falsified.",
            "evidence": ["controlled probe was negative"],
        },
    )
    quiesce_all(held)
    audit = held.active_audit()
    assert audit is not None
    valid = AuditDecision(
        audit_id=audit["id"],
        revision=audit["revision"],
        lanes=[
            {
                "lane": "lane-2",
                "verdict": "redirect",
                "reason": "Negative evidence exhausts the current approach.",
                "replacement": mission("orthogonal replacement", "architecture"),
            }
        ],
    )
    stale = valid.model_copy(update={"revision": valid.revision + 1})
    session = AuditSequence([stale, valid])
    decided, attempts = _run_audit_session(
        session, held.decision_packet({}, {"status": "running"})
    )
    assert decided == valid
    assert len(session.prompts) == 2
    assert attempts[0]["error"].startswith("revision must be")
    assert attempts[1]["valid"] is True

    exhausted = AuditSequence([stale, stale, stale])
    fallback, failed_attempts = _run_audit_session(
        exhausted, held.decision_packet({}, {"status": "running"})
    )
    assert fallback is None
    assert len(failed_attempts) == 3


def test_audit_prompt_projects_oversized_history_within_a_hard_budget() -> None:
    held = controller()
    identity = held.identity("lane-2")
    held.observe(
        "lane-2",
        {
            **identity,
            "status": "no_result",
            "summary": "The hypothesis was falsified.",
            "evidence": ["controlled probe was negative"],
        },
    )
    quiesce_all(held)
    packet = held.decision_packet({}, {"status": "running"})
    audit = packet["audit"]
    assert isinstance(audit, dict)
    audit["decision_attempts"] = [
        {"number": index, "error": "oversized backend failure " + "x" * 30_000}
        for index in range(40)
    ]
    packet["latest_reports"] = {
        "lane-2": {"summary": "verbose evidence " + "y" * 900_000}
    }
    packet["recent_audits"] = [json.loads(json.dumps(audit)) for _ in range(10)]
    packet["manifest"] = {
        "status": "running",
        "active_audit": json.loads(json.dumps(audit)),
        "integration_queue": [{"notes": "z" * 900_000}],
    }

    projected = compact_audit_packet(packet)
    rendered = audit_prompt(projected)
    projected_audit = projected["audit"]
    assert isinstance(projected_audit, dict)
    assert len(rendered) <= AUDIT_PROMPT_MAX_CHARS
    assert projected_audit["id"] == audit["id"]
    assert projected_audit["revision"] == audit["revision"]
    assert projected_audit["targets"] == audit["targets"]
    assert projected_audit["decision_attempts"] == []
    assert projected_audit["decision_attempt_summary"]["count"] == 40
    assert projected["active_missions"]["lane-2"]["id"] == identity["mission_id"]


class InputTooLargeOnce:
    def __init__(self, decision: AuditDecision) -> None:
        self.decision = decision
        self.prompts: list[str] = []

    def __call__(
        self, prompt: str, *, suppress: bool, schema: type[Any]
    ) -> AuditDecision:
        assert suppress is False
        assert schema is AuditDecision
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            raise RuntimeError(
                '{"input_error_code":"input_too_large",'
                '"message":"Input exceeds the maximum length"}'
            )
        return self.decision


def test_audit_input_too_large_retries_immediately_with_compact_packet() -> None:
    held = controller()
    identity = held.identity("lane-2")
    held.observe(
        "lane-2",
        {
            **identity,
            "status": "no_result",
            "summary": "The hypothesis was falsified.",
            "evidence": ["controlled probe was negative"],
        },
    )
    quiesce_all(held)
    audit = held.active_audit()
    assert audit is not None
    valid = AuditDecision(
        audit_id=audit["id"],
        revision=audit["revision"],
        lanes=[
            {
                "lane": "lane-2",
                "verdict": "redirect",
                "reason": "Negative evidence exhausts the current approach.",
                "replacement": mission("orthogonal replacement", "architecture"),
            }
        ],
    )
    session = InputTooLargeOnce(valid)
    decided, attempts = _run_audit_session(
        session, held.decision_packet({}, {"status": "running"})
    )
    assert decided == valid
    assert len(session.prompts) == 2
    assert len(session.prompts[0]) <= AUDIT_PROMPT_MAX_CHARS
    assert len(session.prompts[1]) <= AUDIT_PROMPT_RETRY_MAX_CHARS
    assert "input_too_large" in attempts[0]["error"]
    assert attempts[1]["valid"] is True


def test_checkpoint_recovery_requires_exact_generation(tmp_path: Path) -> None:
    path = tmp_path / "lane-2.json"
    checkpoint = LaneCheckpoint(
        identity=CheckpointIdentity(
            run_id="run-1",
            lane="lane-2",
            mission_id="M000002",
            generation=4,
            phase="working",
            updated_at=dt.datetime(2026, 8, 23, tzinfo=dt.UTC),
        ),
        report=LaneReport(
            status="progress",
            summary="A partial controlled probe landed.",
            evidence=["checkpoint evidence"],
        ),
    )
    path.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    exact = {
        "version": 1,
        "run_id": "run-1",
        "lane": "lane-2",
        "mission_id": "M000002",
        "generation": 4,
    }
    assert _checkpoint_report(path, None, exact) == checkpoint.report
    assert _checkpoint_report(path, None, {**exact, "generation": 5}) is None


def test_source_lock_rejects_a_second_integration_owner(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    lock_path = tmp_path / "source.lock"
    with (
        SourceLock(lock_path, source, "run-1"),
        pytest.raises(RuntimeError, match="another parallel Flame Chase"),
        SourceLock(lock_path, source, "run-2"),
    ):
        pass


def test_runtime_control_log_cannot_be_replaced_with_a_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    paths = RunPaths(tmp_path / "run", source)
    paths.root.mkdir()
    initialize_paths(paths, make_snapshots=True)
    validate_runtime_layout(paths)
    report = paths.reports / "lane-2.jsonl"
    report.unlink()
    report.symlink_to(tmp_path / "outside.jsonl")
    with pytest.raises(RuntimeError, match="replaced or linked"):
        validate_runtime_layout(paths)
