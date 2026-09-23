"""Structured legacy orchestrateor PR-review result for Git/PR mode."""

from __future__ import annotations

from typing import Literal

from _parallel_flame_chase.core.models import ReportItem, StrictModel
from pydantic import Field, model_validator


class PRReviewResult(StrictModel):
    """What the orchestrateor did in one fresh PR-review session."""

    pr_id: str = Field(min_length=1, max_length=100)
    verdict: Literal["merged", "rejected", "continue"]
    summary: str = Field(min_length=1, max_length=4000)
    evidence: list[ReportItem] = Field(default_factory=list, max_length=30)
    merge_commit: str | None = Field(default=None, max_length=100)
    evaluation_receipt_ids: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def merge_result_has_commit(self) -> PRReviewResult:
        if self.verdict == "merged" and not self.merge_commit:
            raise ValueError("a merged review requires merge_commit")
        if self.verdict != "merged" and self.merge_commit is not None:
            raise ValueError("only a merged review may carry merge_commit")
        return self


__all__ = [
    "PRReviewResult",
]
