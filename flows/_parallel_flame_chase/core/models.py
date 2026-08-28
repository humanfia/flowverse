"""Strict public and durable shapes for the parallel Flame Chase."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LaneName = Literal["lane-1", "lane-2", "lane-3"]
LANES: tuple[LaneName, LaneName, LaneName] = ("lane-1", "lane-2", "lane-3")
MissionKind = Literal["research", "implementation", "validation", "integration"]
ChangeScale = Literal["probe", "component", "architecture", "validation", "integration"]
ReportItem = Annotated[str, Field(min_length=1, max_length=2000)]
Criterion = Annotated[str, Field(min_length=1, max_length=2000)]
Dependency = Annotated[str, Field(min_length=1, max_length=500)]


def _require_every_property(schema: dict[str, Any]) -> None:
    """Make each object compatible with Codex/OpenAI strict structured output.

    Pydantic omits fields with defaults from ``required``. Codex strict output instead requires
    every declared property to be present, including nullable and defaulted ones. Applying this
    hook on the shared base also covers nested definitions such as ``MissionSpec``.
    """
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)


class StrictModel(BaseModel):
    """A runtime contract rejects unknown fields rather than silently losing them."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_every_property)


class ArtifactRef(StrictModel):
    """One explicit file a lane publishes under its shared artifact directory."""

    path: str = Field(min_length=1, max_length=1000)
    description: str = Field(min_length=1, max_length=2000)

    @field_validator("path")
    @classmethod
    def relative_file(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("artifact paths must use portable forward-slash syntax")
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError(
                "artifact paths must be relative and stay inside the lane root"
            )
        if value.endswith("/"):
            raise ValueError("artifact paths must name files")
        return value


class Deliverable(StrictModel):
    """A reconstructable result offered to the coordinator and integration lane."""

    title: str = Field(min_length=1, max_length=200)
    approach_class: str = Field(min_length=1, max_length=200)
    artifacts: list[ArtifactRef] = Field(min_length=1, max_length=20)
    integration_notes: str = Field(min_length=1, max_length=8000)
    validation: list[ReportItem] = Field(default_factory=list, max_length=30)


class CandidateSubmission(StrictModel):
    """One evaluator-accepted candidate offered to the shared leaderboard."""

    title: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=200)
    value: float
    direction: Literal["minimize", "maximize"]
    evaluator: str = Field(min_length=1, max_length=1000)
    evidence: list[ReportItem] = Field(default_factory=list, max_length=30)

    @field_validator("value", mode="before")
    @classmethod
    def numeric_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError(  # noqa: TRY004 - Pydantic validators require ValueError
                "candidate value must be numeric, not boolean"
            )
        return value

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("candidate value must be finite")
        return value

    @field_validator("title", "metric", "evaluator")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("candidate text fields must not be blank")
        return stripped


class LaneReport(StrictModel):
    """The complete result of one actor turn."""

    status: Literal[
        "progress", "deliverable_ready", "no_result", "blocked", "turn_failed"
    ]
    summary: str = Field(min_length=1, max_length=8000)
    changes: list[ReportItem] = Field(default_factory=list, max_length=50)
    evidence: list[ReportItem] = Field(default_factory=list, max_length=50)
    tests: list[ReportItem] = Field(default_factory=list, max_length=50)
    risks: list[ReportItem] = Field(default_factory=list, max_length=30)
    next_step: str = Field(default="", max_length=4000)
    deliverable: Deliverable | None = None
    submission: CandidateSubmission | None = None

    @model_validator(mode="after")
    def deliverable_matches_status(self) -> LaneReport:
        if self.status == "deliverable_ready" and self.deliverable is None:
            raise ValueError("deliverable_ready requires a deliverable")
        if self.status != "deliverable_ready" and self.deliverable is not None:
            raise ValueError("only deliverable_ready may carry a deliverable")
        if self.submission is not None and self.deliverable is None:
            raise ValueError(
                "a candidate submission requires a reconstructable deliverable"
            )
        return self


class CheckpointIdentity(StrictModel):
    """The runtime identity an actor-authored checkpoint must match."""

    version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=200)
    lane: LaneName
    mission_id: str | None = Field(default=None, max_length=100)
    generation: int = Field(ge=0)
    phase: Literal["working", "terminal"]
    updated_at: datetime


class LaneCheckpoint(StrictModel):
    """A report written during a turn and usable only under its exact identity."""

    version: Literal[1] = 1
    identity: CheckpointIdentity
    report: LaneReport


class MissionSpec(StrictModel):
    """A falsifiable unit of work assigned to one lane."""

    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=8000)
    success_criteria: list[Criterion] = Field(min_length=1, max_length=12)
    kind: MissionKind = "research"
    approach_class: str = Field(min_length=1, max_length=200)
    change_scale: ChangeScale = "component"
    information_question: str = Field(min_length=1, max_length=2000)
    dependencies: list[Dependency] = Field(default_factory=list, max_length=20)
    deadline_hours: float = Field(default=6.0, ge=0.25, le=168)
    max_turns_without_outcome: int = Field(default=6, ge=1, le=50)


class LaneBrief(StrictModel):
    """The coordinator's initial assignment for one fixed lane."""

    lane: LaneName
    mission: MissionSpec


class InitialPlan(StrictModel):
    """Exactly one distinct initial mission for every lane."""

    lanes: list[LaneBrief] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def exactly_the_three_lanes(self) -> InitialPlan:
        names = [brief.lane for brief in self.lanes]
        if len(set(names)) != len(names) or set(names) != set(LANES):
            raise ValueError(
                "initial plan must contain lane-1, lane-2, and lane-3 once"
            )
        return self
