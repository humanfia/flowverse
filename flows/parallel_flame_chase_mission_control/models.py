"""Strict initial audit-policy models for Mission Control."""

from __future__ import annotations

from typing import Literal

from _parallel_flame_chase.core.models import InitialPlan, ReportItem, StrictModel
from pydantic import Field, model_validator

AuditAction = Literal["off", "targeted", "global", "original"]
AuditCondition = Literal[
    "shared_best_updated",
    "deliverable_ready",
    "no_result",
    "blocked",
    "turn_stall",
    "mission_deadline",
    "actor_pair_blocked",
    "invalid_deliverable",
    "external_review_requested",
    "objective_revision",
    "periodic_review",
]

AUDIT_CONDITIONS: tuple[AuditCondition, ...] = (
    "shared_best_updated",
    "deliverable_ready",
    "no_result",
    "blocked",
    "turn_stall",
    "mission_deadline",
    "actor_pair_blocked",
    "invalid_deliverable",
    "external_review_requested",
    "objective_revision",
    "periodic_review",
)


class EvaluationProfile(StrictModel):
    """Task constraints the orchestrateor used when choosing audit pressure."""

    candidate_submission_limit: int | None = Field(default=None, ge=1)
    evaluator_call_limit: int | None = Field(default=None, ge=1)
    estimated_evaluator_minutes: float | None = Field(default=None, gt=0, le=10080)
    feedback_latency_minutes: float | None = Field(default=None, ge=0, le=10080)
    experiment_time_budget_hours: float | None = Field(default=None, gt=0, le=720)
    evidence: list[ReportItem] = Field(default_factory=list, max_length=30)
    unknowns: list[ReportItem] = Field(default_factory=list, max_length=20)


class AuditRule(StrictModel):
    """One explicit response to one currently supported audit condition."""

    condition: AuditCondition
    action: AuditAction
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def scope_is_actionable(self) -> AuditRule:
        if (
            self.condition in {"objective_revision", "periodic_review"}
            and self.action == "targeted"
        ):
            raise ValueError(f"{self.condition} cannot select a lane-targeted audit")
        return self


class AuditPolicy(StrictModel):
    """The immutable first-start audit policy used for the rest of one run."""

    summary: str = Field(min_length=1, max_length=4000)
    evaluation_profile: EvaluationProfile
    rules: list[AuditRule] = Field(
        min_length=len(AUDIT_CONDITIONS),
        max_length=len(AUDIT_CONDITIONS),
    )
    periodic_review_hours: float | None = Field(default=None, ge=0.25, le=168)

    @model_validator(mode="after")
    def exactly_one_rule_per_condition(self) -> AuditPolicy:
        names = [rule.condition for rule in self.rules]
        if len(set(names)) != len(names) or set(names) != set(AUDIT_CONDITIONS):
            raise ValueError(
                "audit policy must decide every supported condition exactly once"
            )
        periodic = next(
            rule for rule in self.rules if rule.condition == "periodic_review"
        )
        if (periodic.action == "off") != (self.periodic_review_hours is None):
            raise ValueError(
                "periodic_review_hours must be null exactly when periodic review is off"
            )
        return self

    def action_for(self, condition: AuditCondition) -> AuditAction:
        return next(rule.action for rule in self.rules if rule.condition == condition)


class MissionControlPlan(StrictModel):
    """One initial lane portfolio plus its durable audit-control policy."""

    plan: InitialPlan
    audit_policy: AuditPolicy


__all__ = [
    "AUDIT_CONDITIONS",
    "AuditAction",
    "AuditCondition",
    "AuditPolicy",
    "AuditRule",
    "EvaluationProfile",
    "MissionControlPlan",
]
