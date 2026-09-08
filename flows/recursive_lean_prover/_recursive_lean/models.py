"""Structured answers and durable node records used by the flow."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

MIN_SUBPROBLEMS = 2

ReferenceName = Literal["TauCeti", "lean-pool", "mathlib-internal"]


class ReferenceUse(BaseModel):
    """Auditable evidence that one mandatory local corpus was consulted."""

    model_config = {"extra": "forbid"}

    source: ReferenceName
    queries: list[str] = Field(
        min_length=1,
        description="exact search terms or commands used in this source",
    )
    files: list[str] = Field(
        min_length=1,
        description="exact local files inspected, or the searched source root on no match",
    )
    conclusion: str = Field(
        min_length=3,
        description="relevant finding or an explicit no-relevant-match conclusion",
    )


class ReferenceAware(BaseModel):
    """Structured stage output that proves all three corpora were considered."""

    reference_use: list[ReferenceUse] = Field(
        min_length=3,
        max_length=3,
        description="exactly one retrieval record for each mandatory source",
    )

    @field_validator("reference_use")
    @classmethod
    def _all_reference_sources(cls, value: list[ReferenceUse]) -> list[ReferenceUse]:
        expected = {"TauCeti", "lean-pool", "mathlib-internal"}
        found = {one.source for one in value}
        if found != expected or len(value) != len(found):
            raise ValueError(
                "reference_use must contain exactly TauCeti, lean-pool, and mathlib-internal"
            )
        return value


class FetchedProblem(BaseModel):
    """Exactly one Lean-Eval leaderboard problem rendered as Markdown."""

    model_config = {"extra": "forbid"}

    problem_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
        description="the one selected Lean-Eval problem id",
    )
    title: str = Field(min_length=1, max_length=300)
    source_url: str = Field(
        description="canonical https://lean-lang.org/eval/problems/<problem-id>/ URL"
    )
    data_url: str = Field(
        description="canonical v2 JSON endpoint independently checked by the controller"
    )
    generated_at: str = Field(
        min_length=1,
        max_length=100,
        description="site-data generation timestamp copied exactly from the v2 JSON",
    )
    statement_revision: int = Field(ge=1)
    module: str = Field(min_length=1, max_length=500)
    markdown: str = Field(
        min_length=100,
        max_length=8000000,
        description="one self-contained Markdown page in the requested leaderboard format",
    )

    @model_validator(mode="after")
    def _one_problem(self) -> FetchedProblem:
        parsed = urlparse(self.source_url)
        expected_path = f"/eval/problems/{self.problem_id}/"
        if (
            parsed.scheme != "https"
            or parsed.netloc != "lean-lang.org"
            or parsed.path != expected_path
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "source_url must be the canonical leaf URL for exactly problem_id"
            )
        expected_data_url = (
            "https://lean-lang.org/eval/site-data/v2/problems/"
            f"{self.problem_id}.json"
        )
        if self.data_url != expected_data_url:
            raise ValueError("data_url must be the canonical v2 endpoint for problem_id")
        headings = re.findall(r"(?m)^# (.+?)\s*$", self.markdown)
        if len(headings) != 1:
            raise ValueError("problem Markdown must contain exactly one top-level heading")
        if headings[0].strip() != self.title.strip():
            raise ValueError("problem Markdown heading must exactly match title")
        problem_rows = re.findall(r"(?m)^\| Problem id \| `([^`]+)` \|\s*$", self.markdown)
        if not problem_rows or any(row != self.problem_id for row in problem_rows):
            raise ValueError(
                "problem Markdown must contain Problem id rows that all match the selected problem"
            )
        leaf_urls = set(
            re.findall(
                r"https://lean-lang\.org/eval/problems/[A-Za-z0-9][A-Za-z0-9_-]*/",
                self.markdown,
            )
        )
        if leaf_urls != {self.source_url}:
            raise ValueError(
                "problem Markdown must reference exactly the selected canonical problem URL"
            )
        required = (
            f"]({self.source_url})",
            f"> Leaderboard data generated: {self.generated_at}",
            "## Leaderboard entry",
            f"| Statement revision | `{self.statement_revision}` |",
            f"| Module | `{self.module}` |",
            "## Data limitations",
        )
        missing = [marker for marker in required if marker not in self.markdown]
        if missing:
            raise ValueError(
                "problem Markdown is missing required single-problem markers: "
                + ", ".join(missing)
            )
        return self


class NaturalProof(ReferenceAware):
    """A natural-language proof produced before any Lean proof is attempted."""

    model_config = {"extra": "forbid"}

    proof: str = Field(
        min_length=20,
        description="complete numbered natural-language proof, including lemma statements",
    )
    key_steps: list[str] = Field(
        min_length=1,
        description="ordered logical spine of the proof",
    )
    unresolved: list[str] = Field(
        description="actual gaps, not named lemmas that the proof explicitly reduces to",
    )


class NaturalAudit(ReferenceAware):
    """An independent reading of a natural-language proof."""

    model_config = {"extra": "forbid"}

    acceptable: bool = Field(
        description="true only if every step follows and the exact theorem is proved"
    )
    first_invalid_step: str = Field(
        description="first unjustified or false step; empty only when acceptable",
    )
    required_changes: list[str] = Field(
        description="repairs required before Lean formalization",
    )

    @property
    def passed(self) -> bool:
        """Whether the answer consistently approves the proof."""
        return (
            self.acceptable
            and not self.first_invalid_step
            and not self.required_changes
        )


class Subproblem(BaseModel):
    """One named theorem node in a decomposition."""

    model_config = {"extra": "forbid"}

    key: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,39}$",
        description="short unique snake_case key",
    )
    title: str = Field(min_length=3, description="human-readable lemma title")
    statement: str = Field(
        min_length=10,
        description="self-contained mathematical statement with all hypotheses",
    )
    lean_statement: str = Field(
        min_length=1,
        max_length=20000,
        description=(
            "one-line exact Lean proposition/type expression, without a theorem name or "
            "proof; this is frozen before child formalization for comparator checking"
        ),
    )
    lean_name: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_']*$",
        description=(
            "bare Lean theorem identifier X; the child must declare it as Submission.X"
        ),
    )
    depends_on: list[str] = Field(
        description="keys of sibling subproblems required by this one",
    )

    @field_validator("lean_statement")
    @classmethod
    def _safe_lean_statement(cls, value: str) -> str:
        """Keep the frozen type usable as one parenthesized Lean term."""
        normalized = value.strip()
        if "\n" in normalized or "\r" in normalized:
            raise ValueError("lean_statement must be a single line")
        if ":=" in normalized:
            raise ValueError(
                "lean_statement must be a type expression, not a declaration"
            )
        return normalized


class SubproblemAudit(BaseModel):
    """One independently checked child statement and frozen Lean type."""

    model_config = {"extra": "forbid"}

    key: str = Field(description="the exact subproblem key being audited")
    acceptable: bool = Field(
        description="whether the prose statement and exact Lean type match and are sound"
    )
    reason: str = Field(description="specific justification or first blocking defect")


class DecompositionAudit(ReferenceAware):
    """Independent gate on the post-proof theorem decomposition."""

    model_config = {"extra": "forbid"}

    acceptable: bool = Field(
        description="true only when the split decision and every child are acceptable"
    )
    nodes: list[SubproblemAudit] = Field(
        description="one audit for every proposed child, or empty for an atomic theorem"
    )
    required_changes: list[str] = Field(
        description="concrete corrections; empty only when acceptable"
    )

    @property
    def passed(self) -> bool:
        """Whether global and per-child verdicts consistently approve."""
        return (
            self.acceptable
            and all(one.acceptable for one in self.nodes)
            and not self.required_changes
        )


def _no_subproblems() -> list[Subproblem]:
    """Give Pydantic a precisely typed fresh default."""
    return []


class Decomposition(ReferenceAware):
    """A proof split, or an explicit decision that the theorem is already atomic."""

    model_config = {"extra": "forbid"}

    should_split: bool = Field(
        description="whether separate named lemmas make the proof safer or reusable"
    )
    rationale: str = Field(description="why this is or is not a useful split")
    subproblems: list[Subproblem] = Field(
        description="two or more nodes when should_split is true, otherwise empty",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Decomposition:
        if self.should_split and len(self.subproblems) < MIN_SUBPROBLEMS:
            raise ValueError("a split needs at least two subproblems")
        if not self.should_split and self.subproblems:
            raise ValueError("an atomic theorem must not list subproblems")
        keys = [one.key for one in self.subproblems]
        if len(keys) != len(set(keys)):
            raise ValueError("subproblem keys must be unique")
        known = set(keys)
        for one in self.subproblems:
            unknown = set(one.depends_on) - known
            if unknown:
                raise ValueError(
                    f"{one.key} has unknown dependencies: {', '.join(sorted(unknown))}"
                )
            if one.key in one.depends_on:
                raise ValueError(f"{one.key} depends on itself")
        return self


class ProvedTheorem(BaseModel):
    """One theorem that must be copied into the wiki."""

    model_config = {"extra": "forbid"}

    name: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_'.]*$",
        description="fully qualified Lean declaration name",
    )
    statement: str = Field(
        min_length=3,
        description="the exact proposition/type established by the declaration",
    )
    lean_file: str = Field(description="repository-relative .lean source path")
    natural_summary: str = Field(
        min_length=10,
        description="short explanation of the mathematical argument",
    )


def _no_theorems() -> list[ProvedTheorem]:
    """Give Pydantic a precisely typed fresh default."""
    return []


class LeanAudit(ReferenceAware):
    """Independent Lean review performed only after the machine comparator gate passes."""

    model_config = {"extra": "forbid"}

    accepted: bool = Field(
        description="true only if the exact requested theorem is proved without loopholes"
    )
    comparator_reran: bool = Field(
        description="whether the reviewer personally reran the configured comparator"
    )
    comparator_passed: bool = Field(
        description="whether that independent rerun exited zero with the required marker"
    )
    proof_matches_statement: bool = Field(
        description="whether no hypothesis, target, import, axiom, or declaration was weakened"
    )
    issues: list[str] = Field(
        description="blocking correctness issues; empty only when accepted",
    )
    theorems: list[ProvedTheorem] = Field(
        description="every new or completed theorem proved for this node",
    )

    @property
    def passed(self) -> bool:
        """Whether the reviewer and its comparator rerun both pass."""
        return (
            self.accepted
            and self.comparator_reran
            and self.comparator_passed
            and self.proof_matches_statement
            and not self.issues
            and bool(self.theorems)
        )


NodeStatus = Literal[
    "queued",
    "planning",
    "natural-proof",
    "natural-review",
    "decomposing",
    "waiting-children",
    "waiting-lean",
    "rlcr-lean",
    "comparing",
    "lean-review",
    "integrating",
    "proved",
    "failed",
    "interrupted",
]


class NodeRecord(BaseModel):
    """One durable theorem node rendered into the live DAG."""

    model_config = {"extra": "forbid"}

    id: str
    parent: str | None = None
    depth: int = 0
    title: str
    statement: str
    lean_statement: str = ""
    lean_name: str = ""
    status: NodeStatus = "queued"
    message: str = ""
    attempts: int = 0
    plan: str = ""
    natural_proof: str = ""
    lean_files: list[str] = Field(default_factory=list)
    worktree: str = ""
    proof_branch: str = ""
    proof_base_commit: str = ""
    candidate_commit: str = ""
    integrated_commit: str = ""
    children: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    theorems: list[str] = Field(default_factory=list)
    updated_at: str = ""


class SolveResult(BaseModel):
    """What a recursive node hands its parent."""

    ok: bool
    node_id: str
    feedback: str = ""
    theorems: list[ProvedTheorem] = Field(default_factory=_no_theorems)
