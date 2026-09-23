"""Structured answers and durable node records used by the flow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MIN_SUBPROBLEMS = 2


class NaturalProof(BaseModel):
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


class NaturalAudit(BaseModel):
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


class DecompositionAudit(BaseModel):
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


class Decomposition(BaseModel):
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


class LeanAudit(BaseModel):
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
