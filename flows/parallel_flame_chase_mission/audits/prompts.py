"""Bounded prompts used only by the mission audit coordinator."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

AUDIT_PROMPT_MAX_CHARS = 750_000
AUDIT_PROMPT_RETRY_MAX_CHARS = 300_000


def _document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = f"\n… [prompt projection omitted {len(value) - limit} characters]"
    return value[: max(0, limit - len(marker))] + marker


def _select(source: dict[str, Any], *fields: str) -> dict[str, object]:
    return {field: deepcopy(source.get(field)) for field in fields}


def _bound(
    value: Any,
    *,
    text_limit: int,
    list_limit: int,
    path: tuple[str, ...] = (),
) -> Any:
    """Recursively bound verbose evidence without truncating audit targets."""
    if isinstance(value, str):
        return _trim(value, text_limit)
    if isinstance(value, list):
        if path == ("audit", "targets"):
            return list(value)
        kept = [
            _bound(
                item,
                text_limit=text_limit,
                list_limit=list_limit,
                path=(*path, str(index)),
            )
            for index, item in enumerate(value[:list_limit])
        ]
        if len(value) > list_limit:
            kept.append({"_prompt_projection_omitted_items": len(value) - list_limit})
        return kept
    if isinstance(value, dict):
        return {
            str(key): _bound(
                item,
                text_limit=text_limit,
                list_limit=list_limit,
                path=(*path, str(key)),
            )
            for key, item in value.items()
        }
    return value


def _summarize(packet: dict[str, object]) -> dict[str, object]:
    """Collapse retry history before applying the generic recursive bounds."""
    projected = deepcopy(packet)
    audit = projected.get("audit")
    attempt_count = 0
    if isinstance(audit, dict):
        attempts = audit.get("decision_attempts")
        if isinstance(attempts, list):
            attempt_count = len(attempts)
            audit["decision_attempts"] = []
            audit["decision_attempt_summary"] = {
                "count": attempt_count,
                "latest": deepcopy(attempts[-1]) if attempts else None,
            }

    prior = projected.pop("_prompt_projection", None)
    source_chars = prior.get("source_chars") if isinstance(prior, dict) else None
    projected["_prompt_projection"] = {
        "source_chars": source_chars or len(_document(packet)),
        "decision_attempts_omitted": attempt_count,
    }
    return projected


def _render(packet: dict[str, object]) -> str:
    return f"""You are a fresh, read-only coordinator deciding one revision of a generic parallel
Flame Chase audit. Read the mounted `parallel-flame-chase-mission` skill and its mission-audit
reference.
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
    """Build a deterministic coordinator packet that fits the prompt budget."""
    if max_prompt_chars < 20_000:
        raise ValueError("audit prompt budget must be at least 20000 characters")
    if packet.get("_prompt_projection") and len(_render(packet)) <= max_prompt_chars:
        return deepcopy(packet)

    base = _summarize(packet)
    for text_limit, list_limit in (
        (24_000, 128),
        (12_000, 64),
        (6_000, 32),
        (3_000, 16),
        (1_500, 8),
        (750, 4),
        (320, 2),
        (160, 1),
    ):
        candidate = _bound(base, text_limit=text_limit, list_limit=list_limit)
        metadata = candidate.get("_prompt_projection")
        if isinstance(metadata, dict):
            metadata.update(
                max_prompt_chars=max_prompt_chars,
                text_limit=text_limit,
                list_limit=list_limit,
            )
        if len(_render(candidate)) <= max_prompt_chars:
            return candidate

    return _identity_packet(base, packet, max_prompt_chars)


def _identity_packet(
    summarized: dict[str, object],
    original: dict[str, object],
    max_prompt_chars: int,
) -> dict[str, object]:
    """Retain only decision identity and target evidence as a final fallback."""
    raw_audit = summarized.get("audit")
    audit = raw_audit if isinstance(raw_audit, dict) else {}
    raw_missions = summarized.get("active_missions")
    missions = raw_missions if isinstance(raw_missions, dict) else {}
    raw_reports = summarized.get("latest_reports")
    reports = raw_reports if isinstance(raw_reports, dict) else {}
    raw_targets = audit.get("targets")
    targets = raw_targets if isinstance(raw_targets, list) else []
    emergency = {
        "version": summarized.get("version"),
        "protocol": summarized.get("protocol"),
        "run_id": summarized.get("run_id"),
        "objective": _trim(str(summarized.get("objective", "")), 2_000),
        "audit": _select(
            audit,
            "id",
            "scope",
            "targets",
            "revision",
            "status",
            "trigger_kind",
            "requested_at",
            "updated_at",
        ),
        "active_missions": {
            str(lane): _bound(missions.get(lane), text_limit=500, list_limit=2)
            for lane in targets
        },
        "latest_reports": {
            str(lane): _bound(reports.get(lane), text_limit=500, list_limit=2)
            for lane in targets
        },
        "_prompt_projection": {
            "source_chars": len(_document(original)),
            "max_prompt_chars": max_prompt_chars,
            "emergency_identity_projection": True,
        },
    }
    if len(_render(emergency)) > max_prompt_chars:
        raise RuntimeError("audit identity packet exceeds the configured prompt budget")
    return emergency


def audit_prompt(
    packet: dict[str, object],
    *,
    max_chars: int = AUDIT_PROMPT_MAX_CHARS,
) -> str:
    """Ask a fresh coordinator to decide one immutable audit revision."""
    return _render(compact_audit_packet(packet, max_prompt_chars=max_chars))


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
]
