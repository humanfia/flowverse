"""Audit-decision validation and coordinator-session retries."""

from __future__ import annotations

import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, cast

from _parallel_flame_chase.core.utils import now
from hmz.flows import Session, Stopped

from ..coordination.models import AcceptDecision, AuditDecision
from .prompts import (
    AUDIT_PROMPT_MAX_CHARS,
    AUDIT_PROMPT_RETRY_MAX_CHARS,
    audit_prompt,
    audit_repair_prompt,
    compact_audit_packet,
)

AUDIT_RETRY_DELAYS = (5.0, 15.0, 60.0, 300.0)


@dataclass(slots=True)
class CoordinatorRuntime:
    """One fresh audit session and the revision it was asked to decide."""

    future: Future[tuple[AuditDecision | None, list[dict[str, object]]]] | None = None
    session: Session | None = None
    audit_id: str | None = None
    revision: int | None = None
    retry_count: int = 0
    retry_after: float = 0.0
    closed_for_stale_revision: bool = False

    def retry_later(self) -> None:
        delay = AUDIT_RETRY_DELAYS[min(self.retry_count, len(AUDIT_RETRY_DELAYS) - 1)]
        self.retry_count += 1
        self.retry_after = time.monotonic() + delay
        self.closed_for_stale_revision = False

    def reset_retry(self) -> None:
        self.retry_count = 0
        self.retry_after = 0.0
        self.closed_for_stale_revision = False


def _identity_error(audit: dict[str, Any], decision: AuditDecision) -> str | None:
    if decision.audit_id != audit.get("id"):
        return f"audit_id must be {audit.get('id')}"
    if decision.revision != audit.get("revision"):
        return f"revision must be {audit.get('revision')}"
    targets = audit.get("targets")
    if not isinstance(targets, list):
        return "the audit target list is malformed"
    named = [item.lane for item in decision.lanes]
    if len(named) != len(targets) or set(named) != set(targets):
        return f"decisions must name exactly {targets}"
    return None


def _accept_error(mission: dict[str, Any], accepted: AcceptDecision) -> str | None:
    lane = accepted.lane
    outcome = mission.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("outcome") != "deliverable_ready":
        return f"{lane} accept requires a deliverable_ready mission outcome"
    if lane != "lane-1":
        if accepted.integration is None or accepted.next_mission is None:
            return f"{lane} accept requires integration and next_mission"
        if not outcome.get("deliverable") or not outcome.get("artifacts"):
            return f"{lane} accepted deliverable has no validated artifact package"
        return None
    if accepted.integration is not None:
        return "lane-1 accept must not enqueue work to itself"
    spec = mission.get("spec")
    kind = spec.get("kind") if isinstance(spec, dict) else None
    if kind != "integration" and accepted.next_mission is None:
        return "lane-1 non-integration accept requires next_mission"
    if (
        kind == "integration"
        and accepted.next_mission is None
        and not mission.get("resume_mission_id")
    ):
        return "lane-1 integration accept needs a successor when nothing is paused"
    return None


def decision_error(
    audit: object,
    missions: object,
    decision: AuditDecision,
) -> str | None:
    """Validate one decision against an immutable audit and mission view."""
    if not isinstance(audit, dict) or not isinstance(missions, dict):
        return "the evidence packet is malformed"
    error = _identity_error(audit, decision)
    if error is not None:
        return error
    for item in decision.lanes:
        mission = missions.get(item.lane)
        if not isinstance(mission, dict):
            return f"{item.lane} has no active mission in the packet"
        if item.verdict == "accept":
            error = _accept_error(mission, cast("AcceptDecision", item))
            if error is not None:
                return error
    return None


def _input_too_large(error: str) -> bool:
    lowered = error.lower()
    return "input_too_large" in lowered or (
        "input exceeds" in lowered and "maximum length" in lowered
    )


def _compact_retry(packet: dict[str, object]) -> tuple[dict[str, object], str]:
    compact = compact_audit_packet(
        packet,
        max_prompt_chars=AUDIT_PROMPT_RETRY_MAX_CHARS,
    )
    return compact, audit_prompt(compact, max_chars=AUDIT_PROMPT_RETRY_MAX_CHARS)


def run_audit_session(
    session: Session,
    packet: dict[str, object],
) -> tuple[AuditDecision | None, list[dict[str, object]]]:
    """Try one proposal and two same-session repairs before yielding no decision."""
    attempts: list[dict[str, object]] = []
    prompt_packet = compact_audit_packet(
        packet,
        max_prompt_chars=AUDIT_PROMPT_MAX_CHARS,
    )
    prompt = audit_prompt(prompt_packet, max_chars=AUDIT_PROMPT_MAX_CHARS)
    for number in range(1, 4):
        try:
            proposed = session(prompt, suppress=False, schema=AuditDecision)
        except Stopped:
            raise
        except ValueError as why:
            error = f"{type(why).__name__}: {why}"[:2000]
            attempts.append({"number": number, "at": now(), "error": error})
            if _input_too_large(error) and number < 3:
                prompt_packet, prompt = _compact_retry(packet)
            else:
                prompt = audit_repair_prompt(error, prompt_packet)
            continue
        except Exception as why:  # noqa: BLE001 - backend failures are an open set
            error = f"{type(why).__name__}: {why}"[:2000]
            attempts.append({"number": number, "at": now(), "error": error})
            if _input_too_large(error) and number < 3:
                prompt_packet, prompt = _compact_retry(packet)
                continue
            return None, attempts

        error = (
            "coordinator returned no structured decision"
            if proposed is None
            else decision_error(
                prompt_packet.get("audit"),
                prompt_packet.get("active_missions"),
                proposed,
            )
        )
        if proposed is not None and error is None:
            attempts.append({"number": number, "at": now(), "valid": True})
            return proposed, attempts
        attempts.append({"number": number, "at": now(), "error": error})
        prompt = audit_repair_prompt(cast("str", error), prompt_packet)
    return None, attempts
