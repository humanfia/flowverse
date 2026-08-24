"""Prompts that bind agents to the generic collaboration protocol."""

from __future__ import annotations

import json
from typing import Any

AUDIT_PROMPT_MAX_CHARS = 750_000
AUDIT_PROMPT_RETRY_MAX_CHARS = 300_000

_AUDIT_HISTORY_FIELDS = (
    "id",
    "scope",
    "targets",
    "revision",
    "status",
    "trigger_kind",
    "requested_at",
    "updated_at",
    "completed_at",
    "decision",
)
_MANIFEST_FIELDS = (
    "version",
    "protocol",
    "mode",
    "run_id",
    "status",
    "source",
    "objective_fingerprint",
    "updated_at",
    "lanes",
    "external_ingress",
    "remote_actions",
)
_QUEUE_SUMMARY_FIELDS = (
    "id",
    "status",
    "source_lane",
    "source_mission_id",
    "priority",
    "accepted_at",
    "integration_mission_id",
    "started_at",
    "completed_at",
    "reason",
)


def _document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _json_copy(value: object) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _trim_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = f"\n… [prompt projection omitted {len(value) - limit} characters]"
    kept = max(0, limit - len(marker))
    return value[:kept] + marker


def _bound_projection(
    value: Any,
    *,
    text_limit: int,
    list_limit: int,
    path: tuple[str, ...] = (),
) -> Any:
    """Bound verbose evidence while preserving audit identity and target semantics."""
    if isinstance(value, str):
        return _trim_text(value, text_limit)
    if isinstance(value, list):
        if path == ("audit", "targets"):
            return list(value)
        kept = value[:list_limit]
        bounded = [
            _bound_projection(
                item,
                text_limit=text_limit,
                list_limit=list_limit,
                path=(*path, str(index)),
            )
            for index, item in enumerate(kept)
        ]
        if len(value) > list_limit:
            bounded.append(
                {"_prompt_projection_omitted_items": len(value) - list_limit}
            )
        return bounded
    if isinstance(value, dict):
        return {
            str(key): _bound_projection(
                item,
                text_limit=text_limit,
                list_limit=list_limit,
                path=(*path, str(key)),
            )
            for key, item in value.items()
        }
    return value


def _terminal_queue_summary(item: dict[str, Any]) -> dict[str, object]:
    summary = {key: _json_copy(item.get(key)) for key in _QUEUE_SUMMARY_FIELDS}
    directive = item.get("directive")
    if isinstance(directive, dict):
        summary["directive"] = {
            "objective": _json_copy(directive.get("objective")),
        }
    deliverable = item.get("deliverable")
    if isinstance(deliverable, dict):
        summary["deliverable"] = {
            "title": _json_copy(deliverable.get("title")),
            "approach_class": _json_copy(deliverable.get("approach_class")),
        }
    return summary


def _base_audit_projection(packet: dict[str, object]) -> dict[str, object]:
    """Remove duplicated history from a coordinator view, not from durable state."""
    projected = _json_copy(packet)
    audit = projected.get("audit")
    attempt_count = 0
    if isinstance(audit, dict):
        attempts = audit.get("decision_attempts")
        if isinstance(attempts, list):
            attempt_count = len(attempts)
            latest = _json_copy(attempts[-1]) if attempts else None
            audit["decision_attempts"] = []
            audit["decision_attempt_summary"] = {
                "count": attempt_count,
                "latest": latest,
            }

    current_audit_id = audit.get("id") if isinstance(audit, dict) else None
    recent = projected.get("recent_audits")
    recent_summaries: list[dict[str, object]] = []
    recent_count = 0
    if isinstance(recent, list):
        recent_count = len(recent)
        for raw in recent:
            if not isinstance(raw, dict) or raw.get("id") == current_audit_id:
                continue
            recent_summaries.append(
                {key: _json_copy(raw.get(key)) for key in _AUDIT_HISTORY_FIELDS}
            )
    projected["recent_audits"] = recent_summaries[-10:]

    manifest = projected.get("manifest")
    if isinstance(manifest, dict):
        projected["manifest"] = {
            key: _json_copy(manifest.get(key)) for key in _MANIFEST_FIELDS
        }

    queue = projected.get("integration_queue")
    terminal_count = 0
    if isinstance(queue, list):
        compact_queue: list[object] = []
        for raw in queue:
            if not isinstance(raw, dict):
                compact_queue.append(_json_copy(raw))
                continue
            if raw.get("status") in {"accepted", "rejected"}:
                terminal_count += 1
                compact_queue.append(_terminal_queue_summary(raw))
            else:
                compact_queue.append(_json_copy(raw))
        projected["integration_queue"] = compact_queue

    prior_projection = projected.pop("_prompt_projection", None)
    prior_source_chars = (
        prior_projection.get("source_chars")
        if isinstance(prior_projection, dict)
        else None
    )
    projected["_prompt_projection"] = {
        "source_chars": prior_source_chars or len(_document(packet)),
        "decision_attempts_omitted": attempt_count,
        "recent_audits_summarized": recent_count,
        "terminal_integrations_summarized": terminal_count,
    }
    return projected


def _render_audit_prompt(packet: dict[str, object]) -> str:
    return f"""You are a fresh, read-only coordinator deciding one revision of a generic parallel
Flame Chase audit. Read the mounted `parallel-flame-chase` skill and its mission-audit reference.
Inspect the evidence packet and any snapshot available in the working directory. Do not edit any
lane workspace, defend with the actors, run remote actions, or assume evidence absent from the
packet.

Decide exactly the target lanes named by the audit, once each:
- continue: the current mission remains the best next information-bearing work;
- redirect: end it and supply a materially different, falsifiable replacement;
- accept: only for a validated `deliverable_ready` outcome.

For Lane 2 or 3, accept must provide both an integration directive for Lane 1 and a next mission
for that research lane. Lane 1 never enqueues to itself. An accepted Lane 1 integration may resume
its paused research mission by omitting `next_mission`; otherwise define its successor. Bind the
answer to the exact audit_id and revision. Reasons must cite concrete packet evidence.

The packet below is a deterministic prompt projection. Full evidence remains in the packet file
recorded by the runtime; projection metadata states what was summarized or bounded.

Evidence packet:
{_document(packet)}

Return only the structured AuditDecision requested by the runtime.
"""


def compact_audit_packet(
    packet: dict[str, object],
    *,
    max_prompt_chars: int = AUDIT_PROMPT_MAX_CHARS,
) -> dict[str, object]:
    """Build a deterministic coordinator packet that always fits its prompt budget."""
    if max_prompt_chars < 20_000:
        raise ValueError("audit prompt budget must be at least 20000 characters")
    if (
        isinstance(packet.get("_prompt_projection"), dict)
        and len(_render_audit_prompt(packet)) <= max_prompt_chars
    ):
        return _json_copy(packet)
    base = _base_audit_projection(packet)
    levels = (
        (24_000, 128),
        (12_000, 64),
        (6_000, 32),
        (3_000, 16),
        (1_500, 8),
        (750, 4),
        (320, 2),
        (160, 1),
    )
    for text_limit, list_limit in levels:
        candidate = _bound_projection(
            base,
            text_limit=text_limit,
            list_limit=list_limit,
        )
        metadata = candidate.get("_prompt_projection")
        if isinstance(metadata, dict):
            metadata["max_prompt_chars"] = max_prompt_chars
            metadata["text_limit"] = text_limit
            metadata["list_limit"] = list_limit
        if len(_render_audit_prompt(candidate)) <= max_prompt_chars:
            return candidate

    raw_audit = base.get("audit")
    audit = raw_audit if isinstance(raw_audit, dict) else {}
    raw_missions = base.get("active_missions")
    missions = raw_missions if isinstance(raw_missions, dict) else {}
    raw_targets = audit.get("targets")
    targets = raw_targets if isinstance(raw_targets, list) else []
    raw_reports = base.get("latest_reports")
    reports = raw_reports if isinstance(raw_reports, dict) else {}
    emergency = {
        "version": base.get("version"),
        "protocol": base.get("protocol"),
        "run_id": base.get("run_id"),
        "objective": _trim_text(str(base.get("objective", "")), 2_000),
        "audit": {
            key: _json_copy(audit.get(key))
            for key in (
                "id",
                "scope",
                "targets",
                "revision",
                "status",
                "trigger_kind",
                "requested_at",
                "updated_at",
            )
        },
        "active_missions": {
            str(lane): _bound_projection(
                missions.get(lane), text_limit=500, list_limit=2
            )
            for lane in targets
        },
        "latest_reports": {
            str(lane): _bound_projection(
                reports.get(lane),
                text_limit=500,
                list_limit=2,
            )
            for lane in targets
        },
        "_prompt_projection": {
            "source_chars": len(_document(packet)),
            "max_prompt_chars": max_prompt_chars,
            "emergency_identity_projection": True,
        },
    }
    if len(_render_audit_prompt(emergency)) > max_prompt_chars:
        raise RuntimeError("audit identity packet exceeds the configured prompt budget")
    return emergency


def planning_prompt(
    *,
    objective: str,
    workspace_map: dict[str, object],
    mission_mode: bool,
) -> str:
    """Ask the coordinator for three diverse, falsifiable initial lanes."""
    cadence = (
        "Every lane mission will later be audited against an explicit outcome."
        if mission_mode
        else "This is the only coordinator turn; lanes will subsequently self-coordinate "
        "through durable reports."
    )
    return f"""You are the planning coordinator for a generic parallel Flame Chase.

Read the repository and the mounted `parallel-flame-chase` skill before deciding. Plan only:
do not edit the repository, execute remote actions, or start implementation. Split the objective
into exactly three materially different lanes. Lane 1 is the sole integration owner and works in
the original source. Lanes 2 and 3 work in private snapshots and must publish reconstructable
artifact packages. Make each mission falsifiable, information-seeking, and independently useful.
Avoid three cosmetic variants of one approach. {cadence}

Objective:
{objective}

Workspace map:
{_document(workspace_map)}

Return only the structured InitialPlan requested by the runtime.
"""


def lane_prompt(
    *,
    objective: str,
    lane: str,
    actor_role: str,
    turn: int,
    workspace_map: dict[str, object],
    mission: dict[str, object] | None,
    initial_brief: dict[str, object],
    unread_reports: list[dict[str, object]],
    checkpoint_path: str,
    artifact_root: str,
    identity: dict[str, object],
    integration_item: dict[str, object] | None,
    runtime_status: dict[str, object],
) -> str:
    """Build a self-contained fresh-session prompt for one alternating actor."""
    ownership = (
        "You are Lane 1, the sole integration owner. You may edit the original source. "
        "Integrate other lanes only from validated artifact packages and keep the source coherent."
        if lane == "lane-1"
        else "You are a private research lane. Work only in your snapshot. Do not edit the "
        "original source. Publish every offered deliverable as explicit files under your artifact "
        "root, with enough integration notes for Lane 1 to reconstruct it."
    )
    mission_text = (
        _document(mission) if mission is not None else _document(initial_brief)
    )
    integration_text = (
        _document(integration_item)
        if integration_item is not None
        else "No accepted integration package is assigned this turn."
    )
    reports_text = _document(unread_reports) if unread_reports else "[]"
    return f"""You are {actor_role}, taking turn {turn} for {lane} in a generic parallel Flame
Chase. This is a fresh session. Read the repository, TASK.md when present, and the mounted
`parallel-flame-chase` skill. Your partner alternates with you; leave durable work and evidence,
not conversational memory.

{ownership}

Do substantive work now. Test claims proportionally. Do not invoke remote release, deployment,
submission, purchase, or messaging actions: this flow has no remote-action authority. Do not
invent success. A `deliverable_ready` report means the declared files exist and another lane can
reconstruct the result. Use `no_result` when a falsifiable direction has been exhausted, `blocked`
for a concrete external or technical blocker, and `progress` only when another local turn is
worth taking. The runtime records `turn_failed`; do not select it yourself.

Objective:
{objective}

Current mission or base lane brief:
{mission_text}

Lane-local runtime status from the preceding attempt:
{_document(runtime_status)}

Accepted integration work (Lane 1 only):
{integration_text}

Workspace ownership:
{_document(workspace_map)}

Reports from other lanes not yet acknowledged by this lane:
{reports_text}

Your artifact root is `{artifact_root}`. Artifact paths in a deliverable are relative to that
root. You may update `{checkpoint_path}` during meaningful work using the LaneCheckpoint schema
and this exact identity:
{_document(identity)}
The checkpoint is recovery evidence only; it does not trigger audits and it is ignored unless its
identity and generation match this turn.

Finish by returning only the structured LaneReport requested by the runtime. Summarize actual
changes, evidence, tests, risks, and the next useful step.
"""


def lane_repair_prompt(error: str) -> str:
    """Repair an invalid report without discarding the actor's conversation context."""
    return f"""Your preceding answer was rejected by the LaneReport protocol:
{error}

Return only a corrected structured LaneReport for the work you just completed. Preserve the
facts and evidence from that work; do not start another implementation turn. Use status
`deliverable_ready` exactly when `deliverable` is non-null. Every other status requires
`deliverable` to be null. Include every requested field, using empty lists or an empty string
where appropriate. Do not add prose outside the structured report.
"""


def audit_prompt(
    packet: dict[str, object],
    *,
    max_chars: int = AUDIT_PROMPT_MAX_CHARS,
) -> str:
    """Ask a fresh coordinator to decide one immutable audit revision."""
    projected = compact_audit_packet(packet, max_prompt_chars=max_chars)
    return _render_audit_prompt(projected)


def audit_repair_prompt(error: str, packet: dict[str, object]) -> str:
    """Repair a semantic decision in the same coordinator session."""
    raw_audit = packet.get("audit")
    audit = raw_audit if isinstance(raw_audit, dict) else {}
    return f"""Your proposed audit decision was rejected by the protocol:
{error}

Repair only the structured decision. It must target exactly {_document(audit.get("targets", []))},
use audit_id {_document(audit.get("id"))}, and revision {_document(audit.get("revision"))}. Do not
change files or add prose. Return the corrected AuditDecision.
"""


__all__ = [
    "AUDIT_PROMPT_MAX_CHARS",
    "AUDIT_PROMPT_RETRY_MAX_CHARS",
    "audit_prompt",
    "audit_repair_prompt",
    "compact_audit_packet",
    "lane_prompt",
    "lane_repair_prompt",
    "planning_prompt",
]
