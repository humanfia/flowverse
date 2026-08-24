"""Strict decision and external-event models used only by Mission mode."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from _parallel_flame_chase.core.models import (
    Criterion,
    LaneName,
    MissionSpec,
    ReportItem,
    StrictModel,
)
from pydantic import Field, model_validator


class IntegrationDirective(StrictModel):
    """How an accepted research deliverable enters Lane 1's priority queue."""

    priority: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Queue order, where a lower number is integrated earlier.",
    )
    objective: str = Field(min_length=1, max_length=8000)
    success_criteria: list[Criterion] = Field(min_length=1, max_length=12)


class ContinueDecision(StrictModel):
    """Retain the current mission with a fresh deadline and session."""

    lane: LaneName
    verdict: Literal["continue"]
    reason: str = Field(min_length=1, max_length=4096)


class RedirectDecision(StrictModel):
    """Reject the current mission and dispatch a materially different one."""

    lane: LaneName
    verdict: Literal["redirect"]
    reason: str = Field(min_length=1, max_length=4096)
    replacement: MissionSpec


class AcceptDecision(StrictModel):
    """Accept the current result and define what follows from it."""

    lane: LaneName
    verdict: Literal["accept"]
    reason: str = Field(min_length=1, max_length=4096)
    next_mission: MissionSpec | None = None
    integration: IntegrationDirective | None = None


# Strict structured output supports ``anyOf`` but rejects Pydantic's discriminated
# ``oneOf``. Literal verdicts and forbidden extras keep this ordinary union unambiguous.
LaneDecision = ContinueDecision | RedirectDecision | AcceptDecision


class AuditDecision(StrictModel):
    """One coordinator decision bound to one exact audit revision."""

    audit_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)
    lanes: list[LaneDecision] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def no_duplicate_lanes(self) -> AuditDecision:
        names = [decision.lane for decision in self.lanes]
        if len(set(names)) != len(names):
            raise ValueError("audit decision contains a duplicate lane")
        return self


class ExternalEventV1(StrictModel):
    """A bounded adapter event that contributes evidence but no commands."""

    version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    at: datetime
    kind: Literal[
        "progress", "deliverable_ready", "no_result", "blocked", "review_requested"
    ]
    lane: LaneName | None = None
    scope: Literal["targeted", "global"] | None = None
    targets: list[LaneName] = Field(default_factory=list, max_length=3)
    summary: str = Field(min_length=1, max_length=4000)
    evidence: list[ReportItem] = Field(default_factory=list, max_length=30)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def event_has_a_safe_scope(self) -> ExternalEventV1:
        if self.kind in {"deliverable_ready", "no_result", "blocked"}:
            if self.lane is None:
                raise ValueError(f"{self.kind} requires lane")
            if self.scope is not None or self.targets:
                raise ValueError("lane outcome scope is derived by the runtime")
        elif self.kind == "review_requested":
            if self.scope is None:
                raise ValueError("review_requested requires scope")
            if self.scope == "targeted" and not self.targets:
                raise ValueError("targeted review_requested requires targets")
            if self.scope == "global" and self.targets:
                raise ValueError("global review_requested must not list targets")
        elif self.scope is not None or self.targets:
            raise ValueError("progress events do not select an audit scope")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("event targets must not contain duplicates")
        return self
